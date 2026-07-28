import json
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import PushSubscription, Conversation, Message, BotSetting
from app.helpers import get_bot_setting
from app.services.mailchimp_service import mailchimp_service
from app.services.sheets_service import sheets_service
from app.services.fcm_service import fcm_service
from app.services.webpush_service import webpush_service
from app.routers.admin import is_authenticated

router = APIRouter(prefix="/admin/api", tags=["Marketing APIs"])

@router.post("/mailchimp/subscribe")
async def subscribe_mailchimp(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    api_key = await get_bot_setting(db, "mailchimp_api_key")
    list_id = await get_bot_setting(db, "mailchimp_list_id")
    prefix = await get_bot_setting(db, "mailchimp_server_prefix")

    res = await mailchimp_service.add_subscriber(
        email=email,
        name=name,
        api_key=api_key,
        list_id=list_id,
        server_prefix=prefix
    )
    return res

@router.post("/sheets/export-leads")
async def export_leads_to_sheets(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    spreadsheet_id = await get_bot_setting(db, "google_sheets_spreadsheet_id")
    token_json_str = await get_bot_setting(db, "google_sheets_token_json")

    stmt = select(Conversation).order_by(Conversation.created_at.desc())
    res = await db.execute(stmt)
    convs = res.scalars().all()

    exported_count = 0
    errors = []

    for c in convs:
        lead_row = [
            c.id,
            c.sender_name or "Unknown",
            c.platform,
            c.sender_id,
            c.created_at.strftime("%Y-%m-%d %H:%M:%S")
        ]
        result = await sheets_service.append_lead(spreadsheet_id, token_json_str, lead_row)
        if result.get("success"):
            exported_count += 1
        else:
            errors.append(result.get("message"))
            break

    return {
        "success": exported_count > 0,
        "exported_count": exported_count,
        "errors": errors
    }

@router.post("/push/subscribe")
async def save_push_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    data = await request.json()
    endpoint = data.get("endpoint")
    keys = data.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Invalid subscription payload")

    stmt = select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    res = await db.execute(stmt)
    existing = res.scalar_one_or_none()

    if not existing:
        sub = PushSubscription(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=request.headers.get("user-agent", "")
        )
        db.add(sub)
        await db.commit()

    return {"success": True, "message": "Subscription saved"}

@router.post("/push/send")
async def send_push_notification(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
    target: str = Form("all"), # all, fcm, webpush
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = {"fcm": None, "webpush": None}

    # 1. FCM Push
    if target in ["all", "fcm"]:
        fcm_key = await get_bot_setting(db, "fcm_server_key")
        results["fcm"] = await fcm_service.send_push_notification(title, body, server_key=fcm_key)

    # 2. Web Push
    if target in ["all", "webpush"]:
        priv_key = await get_bot_setting(db, "vapid_private_key")
        claims_email = await get_bot_setting(db, "vapid_claims_email")

        stmt = select(PushSubscription)
        res = await db.execute(stmt)
        subs = res.scalars().all()

        webpush_sent = 0
        for sub in subs:
            sub_info = {
                "endpoint": sub.endpoint,
                "keys": {
                    "p256dh": sub.p256dh,
                    "auth": sub.auth
                }
            }
            res_wp = await webpush_service.send_notification(
                sub_info, title, body, private_key=priv_key, claims_email=claims_email
            )
            if res_wp.get("success"):
                webpush_sent += 1

        results["webpush"] = {"success": True, "sent_count": webpush_sent, "total_subscribers": len(subs)}

    return results
