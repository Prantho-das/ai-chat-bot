import json
from fastapi import APIRouter, Request, Response, Depends, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models import Conversation, Message, BotSetting
from app.services.ai_service import ai_service
from app.services.messenger_service import messenger_service
from app.services.log_service import log_service

router = APIRouter(prefix="/webhook/messenger", tags=["Messenger Webhook"])


async def get_fb_tokens(db: AsyncSession) -> tuple[str, str]:
    stmt = select(BotSetting).where(BotSetting.key.in_(["fb_verify_token", "fb_page_access_token"]))
    res = await db.execute(stmt)
    records = res.scalars().all()
    setting_dict = {r.key: r.value for r in records}

    verify_token = setting_dict.get("fb_verify_token", "")
    access_token = setting_dict.get("fb_page_access_token", "")
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


def _extract_user_text(messaging_event: dict) -> str | None:
    """Extract user-readable text from any messaging event type."""

    # 1. Standard text message
    message_data = messaging_event.get("message", {})
    if message_data.get("is_echo"):
        return None

    if "text" in message_data:
        return message_data["text"]

    # 2. Quick reply (button tap) — has payload inside message
    quick_reply = message_data.get("quick_reply")
    if quick_reply:
        return quick_reply.get("payload", "")

    # 3. Attachments — sticker, image, audio, video, file, location
    attachments = message_data.get("attachments")
    if attachments:
        parts = []
        for att in attachments:
            att_type = att.get("type", "unknown")
            payload = att.get("payload", {})

            if att_type == "sticker":
                sticker_id = payload.get("sticker_id", "")
                parts.append(f"[স্টিকার পাঠিয়েছেন]")
            elif att_type == "image":
                parts.append("[ছবি পাঠিয়েছেন]")
            elif att_type == "audio":
                parts.append("[অডিও পাঠিয়েছেন]")
            elif att_type == "video":
                parts.append("[ভিডিও পাঠিয়েছেন]")
            elif att_type == "file":
                parts.append("[ফাইল পাঠিয়েছেন]")
            elif att_type == "location":
                lat = payload.get("coordinates", {}).get("lat", "")
                lng = payload.get("coordinates", {}).get("long", "")
                parts.append(f"[লোকেশন শেয়ার করেছেন: {lat}, {lng}]")
            else:
                parts.append(f"[{att_type} পাঠিয়েছেন]")
        if parts:
            return " ".join(parts)

    # 4. Postback (Get Started button, persistent menu, etc.)
    postback = messaging_event.get("postback")
    if postback:
        return postback.get("title") or postback.get("payload", "")

    # 5. Referral (m.me link clicks)
    referral = messaging_event.get("referral")
    if referral:
        return referral.get("ref", "হ্যালো")

    return None


async def _get_or_create_conversation(db: AsyncSession, sender_id: str) -> "Conversation":
    """Get existing conversation or create new one for a messenger sender."""
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

    return conversation


async def _process_dm(sender_id: str, user_text: str, access_token: str, db: AsyncSession):
    """Process a single DM: save message, generate AI reply, send back."""
    try:
        await log_service.log("INFO", "Messenger DM", f"Received DM from {sender_id}: '{user_text[:100]}'")

        conversation = await _get_or_create_conversation(db, sender_id)

        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=user_text,
            is_ai_generated=False
        )
        db.add(user_msg)
        await db.commit()

        stmt_msg = select(Message).where(
            Message.conversation_id == conversation.id
        ).order_by(Message.created_at.asc())
        history_res = await db.execute(stmt_msg)
        history = history_res.scalars().all()

        ai_reply, is_cached, token_stats = await ai_service.generate_response(user_text, history, db)
        await log_service.log("INFO", "AI Engine", f"AI reply for {sender_id}: '{ai_reply[:100]}'")

        ai_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_reply,
            is_ai_generated=True,
            is_cached=is_cached,
            prompt_tokens=token_stats.get("prompt_tokens", 0),
            completion_tokens=token_stats.get("completion_tokens", 0),
            total_tokens=token_stats.get("total_tokens", 0)
        )
        db.add(ai_msg)
        await db.commit()

        await messenger_service.send_text_message(sender_id, ai_reply, access_token)
    except Exception as e:
        await log_service.log("ERROR", "Messenger DM", f"Error processing DM from {sender_id}: {e}")
        # Send a fallback reply so the user is not left without a response
        try:
            fallback = "দুঃখিত, এই মুহূর্তে একটি সমস্যা হয়েছে। অনুগ্রহ করে একটু পরে আবার চেষ্টা করুন।"
            await messenger_service.send_text_message(sender_id, fallback, access_token)
        except Exception:
            pass


async def _process_comment(entry_page_id: str, change: dict, access_token: str, db: AsyncSession):
    """Process a single FB comment: save, generate AI reply, reply to comment."""
    val = change.get("value", {})
    comment_id = str(val.get("comment_id") or val.get("id") or "")
    sender_id = str(val.get("from", {}).get("id", ""))
    comment_text = val.get("message") or val.get("comment_text") or val.get("text") or ""

    if not (comment_id and comment_text and sender_id and sender_id != entry_page_id):
        return

    sender_name = val.get("from", {}).get("name", "FB Commenter")
    await log_service.log("INFO", "FB Comment", f"Comment from {sender_name} ({sender_id}): '{comment_text[:100]}'")

    try:
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

        formatted_comment = f"[FB Comment] {comment_text}"

        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=formatted_comment,
            is_ai_generated=False
        )
        db.add(user_msg)
        await db.commit()

        stmt_msg = select(Message).where(
            Message.conversation_id == conversation.id
        ).order_by(Message.created_at.asc())
        history_res = await db.execute(stmt_msg)
        history = history_res.scalars().all()

        ai_reply, is_cached, token_stats = await ai_service.generate_response(comment_text, history, db)
        await log_service.log("INFO", "AI Engine", f"Comment AI reply: '{ai_reply[:100]}'")

        ai_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_reply,
            is_ai_generated=True,
            is_cached=is_cached,
            prompt_tokens=token_stats.get("prompt_tokens", 0),
            completion_tokens=token_stats.get("completion_tokens", 0),
            total_tokens=token_stats.get("total_tokens", 0)
        )
        db.add(ai_msg)
        await db.commit()

        await messenger_service.reply_to_comment(comment_id, ai_reply, access_token)
    except Exception as e:
        await log_service.log("ERROR", "FB Comment", f"Error processing comment {comment_id}: {e}")


async def process_messenger_event(data: dict):
    try:
        await log_service.log("INFO", "Messenger Webhook", "Received webhook payload from Meta", json.dumps(data))
        async with AsyncSessionLocal() as db:
            _, access_token = await get_fb_tokens(db)

            for entry in data.get("entry", []):
                page_id = str(entry.get("id", ""))

                # 1. Handle Messenger Direct Messages (text, attachment, postback, quick reply)
                for messaging_event in entry.get("messaging", []):
                    sender_id = str(messaging_event.get("sender", {}).get("id", ""))
                    if not sender_id or sender_id == page_id:
                        continue

                    user_text = _extract_user_text(messaging_event)
                    if user_text:
                        await _process_dm(sender_id, user_text, access_token, db)
                    else:
                        await log_service.log("DEBUG", "Messenger DM", f"Unhandled event from {sender_id}", json.dumps(messaging_event))

                # 2. Handle Facebook Post Comments Auto-Reply
                for change in entry.get("changes", []):
                    field_name = change.get("field")
                    if field_name in ["feed", "comments", "mention"]:
                        await _process_comment(page_id, change, access_token, db)
    except Exception as e:
        await log_service.log("ERROR", "Messenger Webhook", f"Exception processing event: {e}")


@router.post("")
async def handle_messenger_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    obj_type = data.get("object")
    if obj_type in ["page", "permissions"]:
        background_tasks.add_task(process_messenger_event, data)
    else:
        await log_service.log("WARNING", "Messenger Webhook", f"Ignored webhook object type: '{obj_type}'", str(data))
    return Response(content="EVENT_RECEIVED", status_code=200)
