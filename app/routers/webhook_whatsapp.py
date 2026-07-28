from fastapi import APIRouter, Request, Response, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models import Conversation, Message
from app.services.ai_service import ai_service
from app.services.whatsapp_service import whatsapp_service

router = APIRouter(prefix="/webhook/whatsapp", tags=["WhatsApp Webhook"])

@router.get("")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    if hub_mode == "subscribe" and hub_token == settings.WA_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)

@router.post("")
async def handle_whatsapp_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    data = await request.json()
    
    try:
        entries = data.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                
                for msg_data in messages:
                    if msg_data.get("type") == "text":
                        sender_id = msg_data.get("from") # Phone number
                        user_text = msg_data.get("text", {}).get("body", "")
                        
                        contacts = value.get("contacts", [])
                        sender_name = contacts[0].get("profile", {}).get("name") if contacts else f"WA {sender_id[-4:]}"

                        # Fetch or create conversation
                        stmt = select(Conversation).where(
                            Conversation.platform == "whatsapp",
                            Conversation.sender_id == sender_id
                        )
                        result = await db.execute(stmt)
                        conversation = result.scalar_one_or_none()
                        
                        if not conversation:
                            conversation = Conversation(
                                platform="whatsapp",
                                sender_id=sender_id,
                                sender_name=sender_name
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

                        # History & AI Response
                        stmt_msg = select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                        history_res = await db.execute(stmt_msg)
                        history = history_res.scalars().all()

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


                        # Reply on WhatsApp
                        await whatsapp_service.send_text_message(sender_id, ai_reply)

    except Exception as e:
        print(f"Error handling WA Webhook: {e}")

    return {"status": "ok"}
