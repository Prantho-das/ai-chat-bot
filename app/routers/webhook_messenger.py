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
from app.services.lead_extractor_service import lead_extractor_service

router = APIRouter(prefix="/webhook/messenger", tags=["Messenger Webhook"])


from app.models import Conversation, Message, BotSetting, ChannelAccount, Company

async def get_fb_tokens_for_page(db: AsyncSession, page_id: str = None) -> tuple[str, str, int | None]:
    """Retrieve verify_token, access_token, and company_id for a given page_id or fallback to default settings."""
    if page_id:
        stmt = select(ChannelAccount).where(
            ChannelAccount.platform == "messenger",
            ChannelAccount.platform_account_id == str(page_id),
            ChannelAccount.is_active == True
        )
        res = await db.execute(stmt)
        channel = res.scalar_one_or_none()
        if channel:
            return channel.verify_token or "", channel.access_token or "", channel.company_id

    # Fallback to global bot settings
    stmt = select(BotSetting).where(BotSetting.key.in_(["fb_verify_token", "fb_page_access_token"]))
    res = await db.execute(stmt)
    records = res.scalars().all()
    setting_dict = {r.key: r.value for r in records}

    verify_token = setting_dict.get("fb_verify_token", "")
    access_token = setting_dict.get("fb_page_access_token", "")
    return verify_token, access_token, 1


@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    db: AsyncSession = Depends(get_db)
):
    # Check if hub_token matches global or any channel account
    expected_verify_token, _, _ = await get_fb_tokens_for_page(db)
    if hub_mode == "subscribe" and hub_token == expected_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")

    # Check channels
    stmt = select(ChannelAccount).where(ChannelAccount.verify_token == hub_token, ChannelAccount.is_active == True)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        return Response(content=hub_challenge, media_type="text/plain")

    return Response(content="Verification failed", status_code=403)


def _extract_user_text(messaging_event: dict) -> tuple[str | None, str | None, str | None]:
    """Extract user-readable text, image_url, and audio_url from messaging event."""

    message_data = messaging_event.get("message", {})
    if message_data.get("is_echo"):
        return None, None, None

    user_text = message_data.get("text")
    image_url = None
    audio_url = None

    quick_reply = message_data.get("quick_reply")
    if quick_reply and not user_text:
        user_text = quick_reply.get("payload", "")

    attachments = message_data.get("attachments")
    if attachments:
        for att in attachments:
            att_type = att.get("type", "unknown")
            payload = att.get("payload", {})
            url = payload.get("url")
            if att_type == "image" and url:
                image_url = url
            elif att_type == "audio" and url:
                audio_url = url

    if not user_text and not image_url and not audio_url:
        return None, None, None

    return user_text, image_url, audio_url


async def _get_or_create_conversation(db: AsyncSession, sender_id: str, access_token: str = None, company_id: int = 1) -> "Conversation":
    """Get existing conversation or create new one for a messenger sender."""
    stmt = select(Conversation).where(
        Conversation.platform == "messenger",
        Conversation.sender_id == sender_id
    )
    result = await db.execute(stmt)
    conversation = result.scalar_one_or_none()

    if not conversation:
        user_name = f"FB User {sender_id[-4:]}"
        profile = await messenger_service.get_user_profile(sender_id, access_token)
        if profile and profile.get("name"):
            user_name = profile["name"]

        conversation = Conversation(
            platform="messenger",
            sender_id=sender_id,
            sender_name=user_name,
            company_id=company_id
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    else:
        if company_id and conversation.company_id != company_id:
            conversation.company_id = company_id
        if conversation.sender_name.startswith("FB User"):
            profile = await messenger_service.get_user_profile(sender_id, access_token)
            if profile and profile.get("name"):
                conversation.sender_name = profile["name"]
        await db.commit()

    return conversation


async def _process_dm(sender_id: str, user_text: str, access_token: str, db: AsyncSession, image_url: str = None, audio_url: str = None, company_id: int = 1):
    """Process a single DM: save message, generate AI reply, send back."""
    try:
        # Immediate typing indicator for smooth user experience
        await messenger_service.send_typing_indicator(sender_id, access_token)

        await log_service.log("INFO", "Messenger DM", f"Received DM from {sender_id}: '{user_text[:100]}'")

        conversation = await _get_or_create_conversation(db, sender_id, access_token, company_id=company_id)

        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=user_text,
            is_ai_generated=False
        )
        db.add(user_msg)
        await db.commit()

        await lead_extractor_service.process_chat_lead(
            db=db,
            sender_id=sender_id,
            platform="messenger",
            user_text=user_text,
            sender_name=conversation.sender_name
        )

        # Check if Human Agent has taken over or AI is paused
        if conversation.status in ["paused", "escalated"]:
            await log_service.log("INFO", "Human Takeover", f"AI reply skipped for {sender_id} (Status: {conversation.status})")
            return

        stmt_msg = select(Message).where(
            Message.conversation_id == conversation.id
        ).order_by(Message.created_at.asc())
        history_res = await db.execute(stmt_msg)
        history = history_res.scalars().all()

        image_bytes, image_mime = None, None
        audio_bytes, audio_mime = None, None

        if image_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=15.0) as client:
                    res = await client.get(image_url)
                    if res.status_code == 200:
                        image_bytes = res.content
                        image_mime = res.headers.get("content-type", "image/jpeg")
            except Exception as img_err:
                print(f"[IMAGE FETCH ERROR] {img_err}")

        if audio_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=15.0) as client:
                    res = await client.get(audio_url)
                    if res.status_code == 200:
                        audio_bytes = res.content
                        audio_mime = res.headers.get("content-type", "audio/mp3")
            except Exception as aud_err:
                print(f"[AUDIO FETCH ERROR] {aud_err}")

        ai_reply, is_cached, rag_info = await ai_service.generate_response(
            user_message=user_text,
            history=history,
            db=db,
            image_bytes=image_bytes,
            image_mime=image_mime,
            audio_bytes=audio_bytes,
            audio_mime=audio_mime,
            user_identifier=sender_id,
            company_id=company_id
        )
        await log_service.log("INFO", "AI Engine", f"AI reply for {sender_id}: '{ai_reply[:100]}'")

        token_data = rag_info.get("tokens", {}) if isinstance(rag_info, dict) else {}
        ai_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_reply,
            is_ai_generated=True,
            is_cached=is_cached,
            prompt_tokens=token_data.get("prompt_tokens", 0),
            completion_tokens=token_data.get("completion_tokens", 0),
            total_tokens=token_data.get("total_tokens", 0)
        )
        db.add(ai_msg)
        await db.commit()

        await messenger_service.send_text_message(sender_id, ai_reply, access_token)
    except Exception as e:
        await log_service.log("ERROR", "Messenger DM", f"Error processing DM from {sender_id}: {e}")
        try:
            fallback = "দুঃখিত, এই মুহূর্তে একটি সমস্যা হয়েছে। অনুগ্রহ করে একটু পরে আবার চেষ্টা করুন।"
            await messenger_service.send_text_message(sender_id, fallback, access_token)
        except Exception:
            pass


async def _process_comment(entry_page_id: str, change: dict, access_token: str, db: AsyncSession, company_id: int = 1):
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
                sender_name=sender_name,
                company_id=company_id
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

        ai_reply, is_cached, rag_info = await ai_service.generate_response(comment_text, history, db, company_id=company_id)
        await log_service.log("INFO", "AI Engine", f"Comment AI reply: '{ai_reply[:100]}'")

        token_data = rag_info.get("tokens", {}) if isinstance(rag_info, dict) else {}
        ai_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_reply,
            is_ai_generated=True,
            is_cached=is_cached,
            prompt_tokens=token_data.get("prompt_tokens", 0),
            completion_tokens=token_data.get("completion_tokens", 0),
            total_tokens=token_data.get("total_tokens", 0)
        )
        db.add(ai_msg)
        await db.commit()

        await messenger_service.reply_to_comment(comment_id, ai_reply, access_token)
    except Exception as e:
        await log_service.log("ERROR", "FB Comment", f"Error processing comment {comment_id}: {e}")


import asyncio
from typing import Dict, List

# In-memory Message Debouncing Buffers
_MESSAGE_BUFFERS: Dict[str, List[dict]] = {}
_MESSAGE_TIMERS: Dict[str, asyncio.Task] = {}

async def _debounced_process_dm(sender_id: str, access_token: str, company_id: int = 1):
    await asyncio.sleep(2.0) # Wait 2.0s for follow-up messages
    events = _MESSAGE_BUFFERS.pop(sender_id, [])
    _MESSAGE_TIMERS.pop(sender_id, None)

    if not events:
        return

    text_parts = []
    image_url = None
    audio_url = None

    for ev in events:
        t = ev.get("text")
        if t:
            text_parts.append(t)
        if ev.get("image_url") and not image_url:
            image_url = ev["image_url"]
        if ev.get("audio_url") and not audio_url:
            audio_url = ev["audio_url"]

    combined_text = " ".join(text_parts).strip()
    if not combined_text:
        if image_url:
            combined_text = "[User sent image]"
        elif audio_url:
            combined_text = "[User sent voice message]"

    async with AsyncSessionLocal() as db:
        await _process_dm(
            sender_id=sender_id,
            user_text=combined_text,
            access_token=access_token,
            db=db,
            image_url=image_url,
            audio_url=audio_url,
            company_id=company_id
        )


async def process_messenger_event(data: dict):
    try:
        await log_service.log("INFO", "Messenger Webhook", "Received webhook payload from Meta", json.dumps(data))
        async with AsyncSessionLocal() as db:
            for entry in data.get("entry", []):
                page_id = str(entry.get("id", ""))
                _, access_token, company_id = await get_fb_tokens_for_page(db, page_id)

                # 1. Handle Messenger Direct Messages with Debouncing
                for messaging_event in entry.get("messaging", []):
                    message_data = messaging_event.get("message", {})
                    if message_data.get("is_echo"):
                        recipient_id = str(messaging_event.get("recipient", {}).get("id", ""))
                        admin_text = message_data.get("text", "[Admin sent media/attachment]")
                        
                        stmt = select(Conversation).where(
                            Conversation.platform == "messenger",
                            Conversation.sender_id == recipient_id
                        )
                        res = await db.execute(stmt)
                        conv = res.scalar_one_or_none()
                        if conv:
                            conv.status = "paused"
                            db.add(Message(
                                conversation_id=conv.id,
                                role="assistant",
                                content=f"[Human Agent Reply] {admin_text}",
                                is_ai_generated=False
                            ))
                            await db.commit()
                            await log_service.log("INFO", "Human Agent Takeover", f"Real Admin replied to {recipient_id}. AI Bot paused.", admin_text)
                        continue

                    sender_id = str(messaging_event.get("sender", {}).get("id", ""))
                    if not sender_id or sender_id == page_id:
                        continue

                    user_text, image_url, audio_url = _extract_user_text(messaging_event)
                    if user_text or image_url or audio_url:
                        asyncio.create_task(messenger_service.send_typing_indicator(sender_id, access_token))

                        _MESSAGE_BUFFERS.setdefault(sender_id, []).append({
                            "text": user_text,
                            "image_url": image_url,
                            "audio_url": audio_url
                        })

                        if sender_id in _MESSAGE_TIMERS:
                            _MESSAGE_TIMERS[sender_id].cancel()

                        task = asyncio.create_task(_debounced_process_dm(sender_id, access_token, company_id=company_id))
                        _MESSAGE_TIMERS[sender_id] = task
                    else:
                        await log_service.log("DEBUG", "Messenger DM", f"Unhandled event from {sender_id}", json.dumps(messaging_event))

                # 2. Handle Facebook Post Comments Auto-Reply
                for change in entry.get("changes", []):
                    field_name = change.get("field")
                    if field_name in ["feed", "comments", "mention"]:
                        await _process_comment(page_id, change, access_token, db, company_id=company_id)
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
