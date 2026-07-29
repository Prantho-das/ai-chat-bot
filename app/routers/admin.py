import os
from fastapi import APIRouter, Request, Response, Depends, Form, UploadFile, File, status
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.database import get_db
from app.config import settings
from app.models import Conversation, Message, KnowledgeEntry, BotSetting, Appointment, SystemLog
from app.services.ai_service import ai_service, DEFAULT_FALLBACK_MESSAGE, DEFAULT_SYSTEM_PROMPT
from app.services.log_service import log_service
from app.helpers import get_bot_setting, upsert_bot_setting

router = APIRouter(prefix="/admin", tags=["Admin Panel"])
templates = Jinja2Templates(directory="app/templates")

serializer = URLSafeTimedSerializer(settings.SECRET_KEY)
COOKIE_NAME = "admin_token"
TOKEN_MAX_AGE = 8 * 3600 # 8 Hours

def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME) or request.cookies.get("admin_session")
    if not token:
        return False
    if token == "authenticated": # Backward compatibility fallback
        return True
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
        return data.get("user") == settings.ADMIN_USERNAME
    except (BadSignature, SignatureExpired):
        return False

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == settings.ADMIN_USERNAME and password == settings.ADMIN_PASSWORD:
        response = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        token = serializer.dumps({"user": username})
        response.set_cookie(key=COOKIE_NAME, value=token, httponly=True, max_age=TOKEN_MAX_AGE)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie("admin_session")
    return response

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    total_convs = await db.scalar(select(func.count(Conversation.id))) or 0
    total_msgs = await db.scalar(select(func.count(Message.id))) or 0
    total_kb = await db.scalar(select(func.count(KnowledgeEntry.id))) or 0
    cached_msgs = await db.scalar(select(func.count(Message.id)).where(Message.is_cached == True)) or 0

    stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(10)
    result = await db.execute(stmt)
    recent_conversations = result.scalars().all()

    stats = {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "knowledge_entries": total_kb,
        "cached_messages": cached_msgs
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_conversations": recent_conversations
    })

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_base_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).order_by(KnowledgeEntry.created_at.desc())
    result = await db.execute(stmt)
    entries = result.scalars().all()

    return templates.TemplateResponse("knowledge_base.html", {
        "request": request,
        "entries": entries,
        "saved": request.query_params.get("saved")
    })

@router.post("/knowledge/add")
async def add_knowledge(
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    entry = KnowledgeEntry(category=category, title=title, content=content)
    db.add(entry)
    await db.commit()

    return RedirectResponse(url="/admin/knowledge?saved=1", status_code=status.HTTP_302_FOUND)

@router.post("/knowledge/edit/{entry_id}")
async def edit_knowledge(
    entry_id: int,
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if entry:
        entry.category = category
        entry.title = title
        entry.content = content
        await db.commit()

    return RedirectResponse(url="/admin/knowledge?saved=1", status_code=status.HTTP_302_FOUND)

@router.post("/knowledge/upload")
async def upload_knowledge_file(
    request: Request,
    category: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    try:
        content_bytes = await file.read()
        content_str = content_bytes.decode("utf-8")
        filename = file.filename or "Uploaded Document"

        entry = KnowledgeEntry(
            category=category,
            title=f"File: {filename}",
            content=content_str
        )
        db.add(entry)
        await db.commit()
    except Exception as e:
        print(f"Error reading file upload: {e}")

    return RedirectResponse(url="/admin/knowledge?saved=1", status_code=status.HTTP_302_FOUND)

@router.post("/knowledge/delete/{entry_id}")
async def delete_knowledge(entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
        await db.commit()

    return RedirectResponse(url="/admin/knowledge", status_code=status.HTTP_302_FOUND)

@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(
    request: Request,
    platform: str = None,
    q: str = None,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Conversation)

    if platform and platform != "all":
        stmt = stmt.where(Conversation.platform == platform)

    if q:
        search_filter = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Conversation.sender_name.like(search_filter),
                Conversation.sender_id.like(search_filter)
            )
        )

    stmt = stmt.order_by(Conversation.updated_at.desc())
    result = await db.execute(stmt)
    conversations = result.scalars().all()

    return templates.TemplateResponse("conversations.html", {
        "request": request,
        "conversations": conversations,
        "selected_platform": platform or "all",
        "search_query": q or ""
    })

@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(conv_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt_conv = select(Conversation).where(Conversation.id == conv_id)
    res_conv = await db.execute(stmt_conv)
    conv = res_conv.scalar_one_or_none()

    if not conv:
        return RedirectResponse(url="/admin/conversations", status_code=status.HTTP_302_FOUND)

    stmt_msgs = select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at.asc())
    res_msgs = await db.execute(stmt_msgs)
    messages = res_msgs.scalars().all()

    return templates.TemplateResponse("conversation_detail.html", {
        "request": request,
        "conversation": conv,
        "messages": messages
    })

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(BotSetting)
    result = await db.execute(stmt)
    settings_records = result.scalars().all()
    settings_dict = {s.key: s.value for s in settings_records}

    creds = {
        "gemini_api_key": settings_dict.get("gemini_api_key", settings.GEMINI_API_KEY),
        "gemini_model": settings_dict.get("gemini_model", settings.GEMINI_MODEL),
        "fb_page_access_token": settings_dict.get("fb_page_access_token", settings.FB_PAGE_ACCESS_TOKEN),
        "fb_verify_token": settings_dict.get("fb_verify_token", settings.FB_VERIFY_TOKEN),
        "wa_access_token": settings_dict.get("wa_access_token", settings.WA_ACCESS_TOKEN),
        "wa_phone_number_id": settings_dict.get("wa_phone_number_id", settings.WA_PHONE_NUMBER_ID),
        "wa_verify_token": settings_dict.get("wa_verify_token", settings.WA_VERIFY_TOKEN),
        "google_client_id": settings_dict.get("google_client_id", os.getenv("GOOGLE_CLIENT_ID", "")),
        "google_client_secret": settings_dict.get("google_client_secret", os.getenv("GOOGLE_CLIENT_SECRET", "")),
        "google_refresh_token": settings_dict.get("google_refresh_token", os.getenv("GOOGLE_REFRESH_TOKEN", "")),
        "google_calendar_id": settings_dict.get("google_calendar_id", os.getenv("GOOGLE_CALENDAR_ID", "primary")),
        "google_calendar_token": settings_dict.get("google_calendar_token", ""),
        "response_length": settings_dict.get("response_length", getattr(settings, "RESPONSE_LENGTH", "short")),
        "booking_keywords": settings_dict.get("booking_keywords", ""),
        "fallback_message": settings_dict.get("fallback_message", DEFAULT_FALLBACK_MESSAGE),
        "mailchimp_api_key": settings_dict.get("mailchimp_api_key", getattr(settings, "MAILCHIMP_API_KEY", "")),
        "mailchimp_list_id": settings_dict.get("mailchimp_list_id", getattr(settings, "MAILCHIMP_LIST_ID", "")),
        "mailchimp_server_prefix": settings_dict.get("mailchimp_server_prefix", getattr(settings, "MAILCHIMP_SERVER_PREFIX", "")),
        "ig_access_token": settings_dict.get("ig_access_token", getattr(settings, "IG_ACCESS_TOKEN", "")),
        "ig_verify_token": settings_dict.get("ig_verify_token", getattr(settings, "IG_VERIFY_TOKEN", "")),
        "google_sheets_spreadsheet_id": settings_dict.get("google_sheets_spreadsheet_id", getattr(settings, "GOOGLE_SHEETS_SPREADSHEET_ID", "")),
        "google_sheets_token_json": settings_dict.get("google_sheets_token_json", ""),
        "fcm_server_key": settings_dict.get("fcm_server_key", getattr(settings, "FCM_SERVER_KEY", "")),
        "vapid_public_key": settings_dict.get("vapid_public_key", getattr(settings, "VAPID_PUBLIC_KEY", "")),
        "vapid_private_key": settings_dict.get("vapid_private_key", getattr(settings, "VAPID_PRIVATE_KEY", "")),
        "vapid_claims_email": settings_dict.get("vapid_claims_email", getattr(settings, "VAPID_CLAIMS_EMAIL", "admin@example.com")),
        "gmail_sender_email": settings_dict.get("gmail_sender_email", getattr(settings, "GMAIL_SENDER_EMAIL", "")),
        "gmail_app_password": settings_dict.get("gmail_app_password", getattr(settings, "GMAIL_APP_PASSWORD", ""))
    }

    available_models = ai_service.get_available_models(creds["gemini_api_key"])

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "system_prompt": settings_dict.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
        "fallback_message": creds["fallback_message"],
        "creds": creds,
        "available_models": available_models,
        "saved": request.query_params.get("saved")
    })

@router.post("/settings")
async def update_settings(
    request: Request,
    form_type: str = Form(...),
    system_prompt: str = Form(None),
    response_length: str = Form(None),
    fallback_message: str = Form(None),
    booking_keywords: str = Form(None),
    gemini_api_key: str = Form(None),
    gemini_model: str = Form(None),
    fb_page_access_token: str = Form(None),
    fb_verify_token: str = Form(None),
    wa_access_token: str = Form(None),
    wa_phone_number_id: str = Form(None),
    wa_verify_token: str = Form(None),
    google_client_id: str = Form(None),
    google_client_secret: str = Form(None),
    google_refresh_token: str = Form(None),
    google_calendar_id: str = Form(None),
    google_calendar_token: str = Form(None),
    mailchimp_api_key: str = Form(None),
    mailchimp_list_id: str = Form(None),
    mailchimp_server_prefix: str = Form(None),
    ig_access_token: str = Form(None),
    ig_verify_token: str = Form(None),
    google_sheets_spreadsheet_id: str = Form(None),
    google_sheets_token_json: str = Form(None),
    fcm_server_key: str = Form(None),
    vapid_public_key: str = Form(None),
    vapid_private_key: str = Form(None),
    vapid_claims_email: str = Form(None),
    gmail_sender_email: str = Form(None),
    gmail_app_password: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    if form_type == "prompt":
        if system_prompt is not None:
            await upsert_bot_setting(db, "system_prompt", system_prompt.strip())
        if response_length is not None:
            await upsert_bot_setting(db, "response_length", response_length.strip())
        if fallback_message is not None:
            await upsert_bot_setting(db, "fallback_message", fallback_message.strip())
        await db.commit()

    elif form_type == "credentials":
        raw_creds = {
            "gemini_api_key": gemini_api_key,
            "gemini_model": gemini_model,
            "fb_page_access_token": fb_page_access_token,
            "fb_verify_token": fb_verify_token,
            "wa_access_token": wa_access_token,
            "wa_phone_number_id": wa_phone_number_id,
            "wa_verify_token": wa_verify_token,
        }
        for k, v in raw_creds.items():
            if v is not None and v.strip() != "":
                val = v.strip()
                await upsert_bot_setting(db, k, val)
                if hasattr(settings, k.upper()):
                    setattr(settings, k.upper(), val)
        await db.commit()

    elif form_type == "calendar":
        cal_settings = {
            "google_client_id": google_client_id,
            "google_client_secret": google_client_secret,
            "google_refresh_token": google_refresh_token,
            "google_calendar_id": google_calendar_id,
            "google_calendar_token": google_calendar_token
        }
        for k, v in cal_settings.items():
            if v is not None and v.strip() != "":
                await upsert_bot_setting(db, k, v.strip())
        await db.commit()

    elif form_type == "keywords":
        if booking_keywords is not None:
            await upsert_bot_setting(db, "booking_keywords", booking_keywords.strip())
            await db.commit()

    elif form_type == "marketing":
        mkt_settings = {
            "mailchimp_api_key": mailchimp_api_key,
            "mailchimp_list_id": mailchimp_list_id,
            "mailchimp_server_prefix": mailchimp_server_prefix,
            "ig_access_token": ig_access_token,
            "ig_verify_token": ig_verify_token,
            "google_sheets_spreadsheet_id": google_sheets_spreadsheet_id,
            "google_sheets_token_json": google_sheets_token_json,
            "fcm_server_key": fcm_server_key,
            "vapid_public_key": vapid_public_key,
            "vapid_private_key": vapid_private_key,
            "vapid_claims_email": vapid_claims_email,
            "gmail_sender_email": gmail_sender_email,
            "gmail_app_password": gmail_app_password
        }
        for k, v in mkt_settings.items():
            if v is not None and v.strip() != "":
                await upsert_bot_setting(db, k, v.strip())
        await db.commit()

    return RedirectResponse(url="/admin/settings?saved=1", status_code=status.HTTP_302_FOUND)

@router.get("/appointments", response_class=HTMLResponse)
async def appointments_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Appointment).order_by(Appointment.created_at.desc())
    result = await db.execute(stmt)
    appointments = result.scalars().all()

    return templates.TemplateResponse("appointments.html", {
        "request": request,
        "appointments": appointments,
        "saved": request.query_params.get("saved")
    })

@router.post("/appointments/status/{appointment_id}")
async def update_appointment_status(
    appointment_id: int,
    request: Request,
    status_val: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Appointment).where(Appointment.id == appointment_id)
    res = await db.execute(stmt)
    appt = res.scalar_one_or_none()
    if appt:
        appt.status = status_val
        await db.commit()

    return RedirectResponse(url="/admin/appointments?saved=1", status_code=status.HTTP_302_FOUND)

@router.post("/appointments/delete/{appointment_id}")
async def delete_appointment(appointment_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Appointment).where(Appointment.id == appointment_id)
    result = await db.execute(stmt)
    appt = result.scalar_one_or_none()

    if appt:
        await db.delete(appt)
        await db.commit()

    return RedirectResponse(url="/admin/appointments", status_code=status.HTTP_302_FOUND)

@router.post("/calendar/upload-json")
async def upload_calendar_json(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    try:
        content_bytes = await file.read()
        json_str = content_bytes.decode("utf-8")
        await upsert_bot_setting(db, "google_calendar_token", json_str)
        await db.commit()
    except Exception as e:
        print(f"Error uploading Google Calendar JSON key: {e}")

    return RedirectResponse(url="/admin/settings?saved=1", status_code=status.HTTP_302_FOUND)

@router.get("/logs", response_class=HTMLResponse)
async def live_logs_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(SystemLog).order_by(SystemLog.created_at.desc()).limit(100)
    res = await db.execute(stmt)
    db_logs = res.scalars().all()

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": db_logs
    })

@router.get("/api/logs")
async def get_live_logs_api(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return {"error": "Unauthorized"}

    mem_logs = log_service.get_recent_memory_logs(limit=50)
    if mem_logs:
        return {"logs": mem_logs}

    stmt = select(SystemLog).order_by(SystemLog.created_at.desc()).limit(50)
    res = await db.execute(stmt)
    db_logs = res.scalars().all()

    formatted_logs = [
        {
            "timestamp": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "level": log.level,
            "source": log.source,
            "message": log.message,
            "details": log.details or ""
        }
        for log in db_logs
    ]
    return {"logs": formatted_logs}
