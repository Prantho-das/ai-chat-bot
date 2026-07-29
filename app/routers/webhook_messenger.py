from fastapi import APIRouter, Request, Response, Depends, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models import Conversation, Message, BotSetting
from app.services.ai_service import ai_service
from app.services.messenger_service import messenger_service

router = APIRouter(prefix="/webhook/messenger", tags=["Messenger Webhook"])

async def get_fb_tokens(db: AsyncSession) -> tuple[str, str]:
    stmt = select(BotSetting).where(BotSetting.key.in_(["fb_verify_token", "fb_page_access_token"]))
    res = await db.execute(stmt)
    records = res.scalars().all()
    setting_dict = {r.key: r.value for r in records}

    verify_token = setting_dict.get("fb_verify_token") or settings.FB_VERIFY_TOKEN
    access_token = setting_dict.get("fb_page_access_token") or settings.FB_PAGE_ACCESS_TOKEN
    return verify_token, access_token


@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    db: AsyncSession = Depends(get_db)
):
    expected_verify_token, _ = await get_fb_tokens(db)

    if hub_mode == "subscribe" and hub_token == expected_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


async def process_messenger_event(data: dict):
    try:
        print(f"[WEBHOOK EVENT RECEIVED] Processing payload: {data}")
        async with AsyncSessionLocal() as db:
            _, access_token = await get_fb_tokens(db)

            for entry in data.get("entry", []):
                page_id = str(entry.get("id", ""))

                # 1. Handle Messenger Direct Inbox Messages
                for messaging_event in entry.get("messaging", []):
                    sender_id = str(messaging_event.get("sender", {}).get("id", ""))
                    message_data = messaging_event.get("message", {})
                    print(f"[MESSENGER EVENT] Sender ID: {sender_id}, Message Data: {message_data}")

                    if sender_id and "text" in message_data and not message_data.get("is_echo"):
                        user_text = message_data["text"]
                        print(f"[MESSENGER INBOX] Text received from {sender_id}: {user_text}")

                        stmt = select(Conversation).where(
                            Conversation.platform == "messenger",
                            Conversation.sender_id == sender_id
                        )
                        result = await db.execute(stmt)
                        conversation = result.scalar_one_or_none()

                        if not conversation:
                            conversation = Conversation(
                                platform="messenger",
                                sender_id=sender_id,
                                sender_name=f"FB User {sender_id[-4:]}"
                            )
                            db.add(conversation)
                            await db.commit()
                            await db.refresh(conversation)

                        user_msg = Message(
                            conversation_id=conversation.id,
                            role="user",
                            content=user_text,
                            is_ai_generated=False
                        )
                        db.add(user_msg)
                        await db.commit()

                        stmt_msg = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                        history_res = await db.execute(stmt_msg)
                        history = history_res.scalars().all()

                        ai_reply, is_cached = await ai_service.generate_response(user_text, history, db)
                        print(f"[AI GENERATED REPLY]: {ai_reply}")

                        ai_msg = Message(
                            conversation_id=conversation.id,
                            role="assistant",
                            content=ai_reply,
                            is_ai_generated=True,
                            is_cached=is_cached
                        )
                        db.add(ai_msg)
                        await db.commit()

                        send_status = await messenger_service.send_text_message(sender_id, ai_reply, access_token)
                        print(f"[FB SEND STATUS]: {send_status}")

                # 2. Handle Facebook Post Comments Auto-Reply (feed changes field)
                for change in entry.get("changes", []):
                    field_name = change.get("field")
                    print(f"[FB CHANGE FIELD]: {field_name}, Data: {change}")
                    if field_name in ["feed", "comments", "mention"]:
                        val = change.get("value", {})
                        verb = val.get("verb", "add")
                        comment_id = str(val.get("comment_id") or val.get("id") or "")
                        sender_id = str(val.get("from", {}).get("id", ""))
                        comment_text = val.get("message") or val.get("comment_text") or val.get("text") or ""

                        if comment_id and comment_text and sender_id and sender_id != page_id:
                            sender_name = val.get("from", {}).get("name", "FB Commenter")
                            print(f"[FB COMMENT RECEIVED] From: {sender_name} ({sender_id}): {comment_text}")

                            stmt = select(Conversation).where(
                                Conversation.platform == "fb_comment",
                                Conversation.sender_id == sender_id
                            )
                            result = await db.execute(stmt)
                            conversation = result.scalar_one_or_none()

                            if not conversation:
                                conversation = Conversation(
                                    platform="fb_comment",
                                    sender_id=sender_id,
                                    sender_name=sender_name
                                )
                                db.add(conversation)
                                await db.commit()
                                await db.refresh(conversation)

                            post_id = val.get("post_id", "")
                            formatted_comment = f"[FB Comment] {comment_text}"

                            user_msg = Message(
                                conversation_id=conversation.id,
                                role="user",
                                content=formatted_comment,
                                is_ai_generated=False
                            )
                            db.add(user_msg)
                            await db.commit()

                            stmt_msg = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                            history_res = await db.execute(stmt_msg)
                            history = history_res.scalars().all()

                            ai_reply, is_cached = await ai_service.generate_response(comment_text, history, db)
                            print(f"[AI COMMENT REPLY]: {ai_reply}")

                            ai_msg = Message(
                                conversation_id=conversation.id,
                                role="assistant",
                                content=ai_reply,
                                is_ai_generated=True,
                                is_cached=is_cached
                            )
                            db.add(ai_msg)
                            await db.commit()

                            comment_send_status = await messenger_service.reply_to_comment(comment_id, ai_reply, access_token)
                            print(f"[FB COMMENT SEND STATUS]: {comment_send_status}")
    except Exception as e:
        print(f"[MESSENGER WEBHOOK ERROR] Error processing event: {e}")


@router.post("")
async def handle_messenger_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    print(f"[WEBHOOK ENTRY POST] Received payload object: {data.get('object')}")
    if data.get("object") in ["page", "permissions"]:
        background_tasks.add_task(process_messenger_event, data)
    else:
        print(f"[WEBHOOK WARNING] Unknown object type: {data.get('object')}")
    return Response(content="EVENT_RECEIVED", status_code=200)
