from fastapi import APIRouter, Request, Response, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
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


@router.post("")
async def handle_messenger_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    data = await request.json()
    _, access_token = await get_fb_tokens(db)

    if data.get("object") == "page":
        for entry in data.get("entry", []):
            page_id = entry.get("id")

            # 1. Handle Messenger Messages
            for messaging_event in entry.get("messaging", []):
                sender_id = messaging_event.get("sender", {}).get("id")
                message_data = messaging_event.get("message", {})

                if sender_id and "text" in message_data and not message_data.get("is_echo"):
                    user_text = message_data["text"]

                    # Fetch or create conversation
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

                    # Store user message
                    user_msg = Message(
                        conversation_id=conversation.id,
                        role="user",
                        content=user_text,
                        is_ai_generated=False
                    )
                    db.add(user_msg)
                    await db.commit()

                    # Fetch previous history
                    stmt_msg = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                    history_res = await db.execute(stmt_msg)
                    history = history_res.scalars().all()

                    # Generate AI Response
                    ai_reply, is_cached = await ai_service.generate_response(user_text, history, db)

                    # Store AI message
                    ai_msg = Message(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=ai_reply,
                        is_ai_generated=True,
                        is_cached=is_cached
                    )
                    db.add(ai_msg)
                    await db.commit()

                    # Send message to Facebook Messenger using DB access token
                    await messenger_service.send_text_message(sender_id, ai_reply, access_token)

            # 2. Handle Post Comments Auto-Reply (feed field)
            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    val = change.get("value", {})
                    verb = val.get("verb")
                    item = val.get("item")
                    comment_id = val.get("comment_id")
                    sender_id = val.get("from", {}).get("id")
                    comment_text = val.get("message", "")

                    # Check item is comment, verb is add/edit, and not posted by the Page itself
                    if comment_id and comment_text and sender_id != page_id:
                        if item in ["comment", "status", "post"] and verb in ["add", "edited", None]:
                            sender_name = val.get("from", {}).get("name", "FB Commenter")

                            # Fetch or Create conversation for comment thread
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
                            formatted_comment = f"[FB Post ID: {post_id}] {comment_text}"

                            user_msg = Message(
                                conversation_id=conversation.id,
                                role="user",
                                content=formatted_comment,
                                is_ai_generated=False
                            )
                            db.add(user_msg)
                            await db.commit()

                            # Generate AI response
                            stmt_msg = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                            history_res = await db.execute(stmt_msg)
                            history = history_res.scalars().all()

                            ai_reply, is_cached = await ai_service.generate_response(comment_text, history, db)

                            ai_msg = Message(
                                conversation_id=conversation.id,
                                role="assistant",
                                content=ai_reply,
                                is_ai_generated=True,
                                is_cached=is_cached
                            )
                            db.add(ai_msg)
                            await db.commit()

                            # Reply directly to the FB comment using DB access token
                            await messenger_service.reply_to_comment(comment_id, ai_reply, access_token)

    return {"status": "ok"}
