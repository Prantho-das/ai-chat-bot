from fastapi import APIRouter, Request, Response, Depends, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models import Conversation, Message, BotSetting
from app.services.ai_service import ai_service
from app.services.instagram_service import instagram_service

router = APIRouter(prefix="/webhook/instagram", tags=["Instagram Webhook"])

async def get_ig_tokens(db: AsyncSession) -> tuple[str, str]:
    stmt = select(BotSetting).where(BotSetting.key.in_(["ig_verify_token", "ig_access_token", "fb_verify_token", "fb_page_access_token"]))
    res = await db.execute(stmt)
    records = res.scalars().all()
    setting_dict = {r.key: r.value for r in records}

    verify_token = setting_dict.get("ig_verify_token") or setting_dict.get("fb_verify_token", "")
    access_token = setting_dict.get("ig_access_token") or setting_dict.get("fb_page_access_token", "")
    return verify_token, access_token


@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    db: AsyncSession = Depends(get_db)
):
    expected_verify_token, _ = await get_ig_tokens(db)

    if hub_mode == "subscribe" and hub_token == expected_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


async def process_instagram_event(data: dict):
    try:
        async with AsyncSessionLocal() as db:
            _, access_token = await get_ig_tokens(db)

            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    sender_id = str(messaging_event.get("sender", {}).get("id", ""))
                    message_data = messaging_event.get("message", {})

                    if sender_id and "text" in message_data and not message_data.get("is_echo"):
                        user_text = message_data["text"]

                        stmt = select(Conversation).where(
                            Conversation.platform == "instagram",
                            Conversation.sender_id == sender_id
                        )
                        result = await db.execute(stmt)
                        conversation = result.scalar_one_or_none()

                        if not conversation:
                            conversation = Conversation(
                                platform="instagram",
                                sender_id=sender_id,
                                sender_name=f"IG User {sender_id[-4:]}"
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

                        ai_msg = Message(
                            conversation_id=conversation.id,
                            role="assistant",
                            content=ai_reply,
                            is_ai_generated=True,
                            is_cached=is_cached
                        )
                        db.add(ai_msg)
                        await db.commit()

                        await instagram_service.send_dm(sender_id, ai_reply, access_token)
    except Exception as e:
        print(f"[INSTAGRAM WEBHOOK ERROR] Error processing event: {e}")


@router.post("")
async def handle_instagram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    if data.get("object") in ["instagram", "page"]:
        background_tasks.add_task(process_instagram_event, data)
    return Response(content="EVENT_RECEIVED", status_code=200)
