import os
from app.config import settings
from fastapi import APIRouter, Request, Response, Depends, Form, UploadFile, File, status
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
try:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    serializer = URLSafeTimedSerializer(settings.SECRET_KEY)
except ImportError:
    class DummySerializer:
        def dumps(self, obj): return "authenticated"
        def loads(self, token, max_age=None): return {"user": settings.ADMIN_USERNAME}
    serializer = DummySerializer()
    BadSignature = Exception
    SignatureExpired = Exception

from app.database import get_db, engine, Base
from app.models import Conversation, Message, KnowledgeEntry, BotSetting, Appointment, SystemLog, Lead, QAIssueReport
from app.services.ai_service import ai_service, DEFAULT_FALLBACK_MESSAGE, DEFAULT_SYSTEM_PROMPT
from app.services.log_service import log_service
from app.helpers import get_bot_setting, upsert_bot_setting
from app.services.lead_extractor_service import lead_extractor_service

router = APIRouter(prefix="/admin", tags=["Admin Panel"])
templates = Jinja2Templates(directory="app/templates")

@router.post("/migrate-db")
async def migrate_db(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await log_service.log("INFO", "DB Migration", "Database schema migration executed successfully by Admin.")
        return RedirectResponse(url="/admin/settings?msg=migration_success", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        await log_service.log("ERROR", "DB Migration", f"Database migration failed: {e}")
        return RedirectResponse(url="/admin/settings?error=migration_failed", status_code=status.HTTP_303_SEE_OTHER)
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

async def render_admin_page(template_name: str, request: Request, db: AsyncSession, context: dict):
    simplified_mode = await get_bot_setting(db, "simplified_client_mode", "false")
    context["simplified_client_mode"] = (simplified_mode == "true")
    context["request"] = request
    try:
        return templates.TemplateResponse(request, template_name, context)
    except TypeError:
        return templates.TemplateResponse(template_name, context)



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

@router.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(BotSetting)
    result = await db.execute(stmt)
    settings_records = result.scalars().all()
    settings_dict = {s.key: s.value for s in settings_records}

    has_gemini = bool(settings_dict.get("gemini_api_key", settings.GEMINI_API_KEY))
    has_fb = bool(settings_dict.get("fb_page_access_token", settings.FB_PAGE_ACCESS_TOKEN))
    has_wa = bool(settings_dict.get("wa_access_token", settings.WA_ACCESS_TOKEN))
    has_calendar = bool(settings_dict.get("google_calendar_token") or settings_dict.get("google_refresh_token") or os.getenv("GOOGLE_REFRESH_TOKEN"))

    return await render_admin_page("guide.html", request, db, {
        "has_gemini": has_gemini,
        "has_fb": has_fb,
        "has_wa": has_wa,
        "has_calendar": has_calendar
    })

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    total_convs = await db.scalar(select(func.count(Conversation.id))) or 0
    total_msgs = await db.scalar(select(func.count(Message.id))) or 0
    total_kb = await db.scalar(select(func.count(KnowledgeEntry.id))) or 0
    cached_msgs = await db.scalar(select(func.count(Message.id)).where(Message.is_cached == True)) or 0
    
    total_prompt_tokens = await db.scalar(select(func.sum(Message.prompt_tokens))) or 0
    total_completion_tokens = await db.scalar(select(func.sum(Message.completion_tokens))) or 0
    total_tokens_used = await db.scalar(select(func.sum(Message.total_tokens))) or 0

    # Gemini 2.0 Flash pricing estimate: $0.10 / 1M input tokens, $0.40 / 1M output tokens
    estimated_cost_usd = (total_prompt_tokens * 0.00000010) + (total_completion_tokens * 0.00000040)
    estimated_cost_bdt = estimated_cost_usd * 122.0

    # Calculate Savings Percentage
    ai_msgs_total = await db.scalar(select(func.count(Message.id)).where(Message.role == "assistant")) or 0
    savings_pct = round((cached_msgs / ai_msgs_total * 100), 1) if ai_msgs_total > 0 else 0.0

    stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(10)
    result = await db.execute(stmt)
    recent_conversations = result.scalars().all()

    stats = {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "knowledge_entries": total_kb,
        "cached_messages": cached_msgs,
        "total_tokens_used": total_tokens_used,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "estimated_cost_usd": f"{estimated_cost_usd:.4f}",
        "estimated_cost_bdt": f"{estimated_cost_bdt:.2f}",
        "savings_pct": savings_pct
    }

    return await render_admin_page("dashboard.html", request, db, {
        "stats": stats,
        "recent_conversations": recent_conversations
    })

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_base_page(
    request: Request,
    q: str = None,
    category: str = None,
    status_filter: str = None,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    # Base query
    stmt = select(KnowledgeEntry)

    if q and q.strip():
        search_term = f"%{q.strip()}%"
        stmt = stmt.where(
            (KnowledgeEntry.title.ilike(search_term)) |
            (KnowledgeEntry.content.ilike(search_term))
        )

    if category and category.strip() and category != "all":
        stmt = stmt.where(KnowledgeEntry.category == category.strip())

    if status_filter == "active":
        stmt = stmt.where(KnowledgeEntry.is_active == True)
    elif status_filter == "inactive":
        stmt = stmt.where(KnowledgeEntry.is_active == False)

    stmt = stmt.order_by(KnowledgeEntry.id.desc())
    result = await db.execute(stmt)
    entries = result.scalars().all()


    # Stats calculation
    all_result = await db.execute(select(KnowledgeEntry))
    all_entries = all_result.scalars().all()
    total_count = len(all_entries)
    active_count = sum(1 for e in all_entries if e.is_active)
    categories_set = set(e.category for e in all_entries if e.category)

    return await render_admin_page("knowledge_base.html", request, db, {
        "entries": entries,
        "total_count": total_count,
        "active_count": active_count,
        "categories_count": len(categories_set),
        "q": q or "",
        "category_filter": category or "all",
        "status_filter": status_filter or "all",
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("error")
    })

@router.post("/knowledge/add")
async def add_knowledge(
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    is_active: bool = Form(False),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    text_to_embed = f"{title.strip()}\n{content.strip()}"
    embedding_vector_json = await ai_service.generate_embedding(text_to_embed, db=db)

    entry = KnowledgeEntry(
        category=category.strip(),
        title=title.strip(),
        content=content.strip(),
        embedding_json=embedding_vector_json,
        is_active=is_active
    )
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
    is_active: bool = Form(False),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if entry:
        entry.category = category.strip()
        entry.title = title.strip()
        entry.content = content.strip()
        entry.is_active = is_active
        
        text_to_embed = f"{title.strip()}\n{content.strip()}"
        entry.embedding_json = await ai_service.generate_embedding(text_to_embed, db=db)
        await db.commit()

    return RedirectResponse(url="/admin/knowledge?saved=1", status_code=status.HTTP_302_FOUND)

@router.post("/knowledge/toggle/{entry_id}")
async def toggle_knowledge_status(entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry:
        entry.is_active = not entry.is_active
        await db.commit()
    return RedirectResponse(url="/admin/knowledge", status_code=status.HTTP_302_FOUND)

@router.post("/knowledge/delete/{entry_id}")
async def delete_knowledge_entry(entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
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
        content_str = None
        for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
            try:
                content_str = content_bytes.decode(encoding)
                break
            except Exception:
                continue

        if not content_str:
            content_str = content_bytes.decode("utf-8", errors="ignore")

        filename = file.filename or "Uploaded Document"
        full_text = content_str.strip()

        embedding_vector_json = await ai_service.generate_embedding(f"{filename}\n{full_text}", db=db)

        entry = KnowledgeEntry(
            category=category.strip(),
            title=f"File: {filename}",
            content=full_text,
            embedding_json=embedding_vector_json
        )
        db.add(entry)
        await db.commit()
        return RedirectResponse(url="/admin/knowledge?saved=1", status_code=status.HTTP_302_FOUND)
    except Exception as e:
        print(f"Error reading file upload: {e}")
        return RedirectResponse(url="/admin/knowledge?error=upload_failed", status_code=status.HTTP_302_FOUND)


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

    return await render_admin_page("conversations.html", request, db, {
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

    return await render_admin_page("conversation_detail.html", request, db, {
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
        "fb_catalog_id": settings_dict.get("fb_catalog_id", ""),
        "wa_access_token": settings_dict.get("wa_access_token", settings.WA_ACCESS_TOKEN),
        "wa_phone_number_id": settings_dict.get("wa_phone_number_id", settings.WA_PHONE_NUMBER_ID),
        "wa_verify_token": settings_dict.get("wa_verify_token", settings.WA_VERIFY_TOKEN),
        "google_client_id": settings_dict.get("google_client_id", os.getenv("GOOGLE_CLIENT_ID", "")),
        "google_client_secret": settings_dict.get("google_client_secret", os.getenv("GOOGLE_CLIENT_SECRET", "")),
        "google_refresh_token": settings_dict.get("google_refresh_token", os.getenv("GOOGLE_REFRESH_TOKEN", "")),
        "google_calendar_id": settings_dict.get("google_calendar_id", os.getenv("GOOGLE_CALENDAR_ID", "primary")),
        "google_calendar_token": settings_dict.get("google_calendar_token", ""),
        "response_length": settings_dict.get("response_length", getattr(settings, "RESPONSE_LENGTH", "short")),
        "company_name": settings_dict.get("company_name", ""),
        "booking_keywords": settings_dict.get("booking_keywords", ""),
        "high_interest_keywords": settings_dict.get("high_interest_keywords", ""),
        "price_keywords": settings_dict.get("price_keywords", ""),
        "detail_keywords": settings_dict.get("detail_keywords", getattr(settings, "DETAIL_KEYWORDS", "")),
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

    return await render_admin_page("settings.html", request, db, {
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
    company_name: str = Form(None),
    response_length: str = Form(None),
    fallback_message: str = Form(None),
    max_history_turns: str = Form(None),
    booking_keywords: str = Form(None),
    detail_keywords: str = Form(None),
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
    simplified_client_mode: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    if form_type == "prompt":
        if company_name is not None:
            await upsert_bot_setting(db, "company_name", company_name.strip())
        if system_prompt is not None:
            await upsert_bot_setting(db, "system_prompt", system_prompt.strip())
        if response_length is not None:
            await upsert_bot_setting(db, "response_length", response_length.strip())
        if fallback_message is not None:
            await upsert_bot_setting(db, "fallback_message", fallback_message.strip())
        if max_history_turns is not None:
            await upsert_bot_setting(db, "max_history_turns", max_history_turns.strip())
        await upsert_bot_setting(db, "simplified_client_mode", "true" if simplified_client_mode == "true" else "false")
        await db.commit()

    elif form_type == "credentials":
        raw_creds = {
            "gemini_api_key": gemini_api_key,
            "gemini_model": gemini_model,
            "fb_page_access_token": fb_page_access_token,
            "fb_verify_token": fb_verify_token,
            "fb_catalog_id": request.form()._dict.get("fb_catalog_id") if hasattr(request.form(), "_dict") else None,
            "wa_access_token": wa_access_token,
            "wa_phone_number_id": wa_phone_number_id,
            "wa_verify_token": wa_verify_token,
        }
        # handle form inputs properly
        form_data = await request.form()
        if "fb_catalog_id" in form_data:
            await upsert_bot_setting(db, "fb_catalog_id", form_data["fb_catalog_id"].strip())

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
        if request.form:
            form_data = await request.form()
            if "high_interest_keywords" in form_data:
                await upsert_bot_setting(db, "high_interest_keywords", form_data["high_interest_keywords"].strip())
            if "price_keywords" in form_data:
                await upsert_bot_setting(db, "price_keywords", form_data["price_keywords"].strip())
        if detail_keywords is not None:
            await upsert_bot_setting(db, "detail_keywords", detail_keywords.strip())
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

    return await render_admin_page("appointments.html", request, db, {
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

    return await render_admin_page("logs.html", request, db, {
        "logs": db_logs
    })

@router.post("/clear-cache")
async def clear_cache(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    from app.models import CacheEntry
    from sqlalchemy import delete
    try:
        await db.execute(delete(CacheEntry))
        await db.commit()
        await log_service.log("SUCCESS", "System", "AI Response Cache was manually cleared by administrator.")
    except Exception as e:
        await log_service.log("ERROR", "System", f"Failed to clear AI Cache: {e}")

    referer = request.headers.get("referer", "/admin/settings")
    # Redirect back to the settings page (or referer) with a success parameter
    redirect_url = referer
    if "saved=" not in redirect_url:
        separator = "&" if "?" in redirect_url else "?"
        redirect_url = f"{redirect_url}{separator}saved=1"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

from app.services.fb_catalog_service import fb_catalog_service

@router.post("/catalog/sync")
async def sync_facebook_catalog(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    res = await fb_catalog_service.sync_catalog_to_meta(db)
    if res.get("success"):
        await log_service.log("SUCCESS", "Facebook Catalog", f"Synced {res.get('synced_count')} products to Meta Catalog.")
    else:
        await log_service.log("ERROR", "Facebook Catalog", f"Sync failed: {res.get('error')}")

    return RedirectResponse(url="/admin/settings?saved=1", status_code=status.HTTP_302_FOUND)

@router.get("/cache-entries")
async def view_cache_entries(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    from app.models import CacheEntry
    from sqlalchemy import select
    stmt = select(CacheEntry).order_by(CacheEntry.created_at.desc()).limit(200)
    res = await db.execute(stmt)
    caches = res.scalars().all()

    return await render_admin_page("cache_entries.html", request, db, {
        "caches": caches
    })

@router.post("/delete-cache-entry/{entry_id}")
async def delete_single_cache_entry(entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    from app.models import CacheEntry
    from sqlalchemy import delete
    try:
        await db.execute(delete(CacheEntry).where(CacheEntry.id == entry_id))
        await db.commit()
        await log_service.log("SUCCESS", "System", f"Cache entry #{entry_id} was deleted.")
    except Exception as e:
        await log_service.log("ERROR", "System", f"Failed to delete cache entry #{entry_id}: {e}")

    return RedirectResponse(url="/admin/cache-entries?saved=1", status_code=status.HTTP_302_FOUND)

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

@router.get("/leads", response_class=HTMLResponse)
async def captured_leads_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Lead).order_by(Lead.id.desc())
    res = await db.execute(stmt)
    leads = res.scalars().all()

    stmt_app = select(Appointment)
    res_app = await db.execute(stmt_app)
    appointments = res_app.scalars().all()

    phone_to_appointment = {}
    for app in appointments:
        if app.customer_phone and app.google_event_link:
            phone_to_appointment[app.customer_phone.strip()] = app.google_event_link

    return await render_admin_page("leads.html", request, db, {
        "leads": leads,
        "phone_to_appointment": phone_to_appointment
    })

# --- Facebook-Styled QA Tester Panel Routes ---

@router.get("/qa-panel", response_class=HTMLResponse)
async def qa_panel_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(QAIssueReport).order_by(QAIssueReport.created_at.desc())
    res = await db.execute(stmt)
    qa_reports = res.scalars().all()

    return await render_admin_page("qa_panel.html", request, db, {
        "qa_reports": qa_reports
    })

@router.post("/api/qa/chat")
async def qa_chat_api(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return {"error": "Unauthorized. Please login to admin dashboard."}

    import time
    start_time = time.time()
    try:
        data = await request.json()
        tester_name = data.get("tester_name", "Tester")
        user_query = data.get("query", "")
        history = data.get("history", [])

        if not user_query:
            return {"error": "Empty query"}

        ai_reply_data = await ai_service.generate_response(
            user_message=user_query,
            history=history,
            db=db
        )

        rag_info = {}
        if isinstance(ai_reply_data, tuple):
            ai_reply = ai_reply_data[0]
            if len(ai_reply_data) >= 3 and isinstance(ai_reply_data[2], dict):
                rag_info = ai_reply_data[2]
        else:
            ai_reply = ai_reply_data

        await lead_extractor_service.process_chat_lead(
            db=db,
            sender_id=f"qa_tester_{tester_name.replace(' ', '_').lower()}",
            platform="messenger",
            user_text=user_query,
            sender_name=tester_name
        )

        elapsed = round(time.time() - start_time, 2)
        return {
            "tester_name": tester_name,
            "query": user_query,
            "ai_response": ai_reply,
            "elapsed_seconds": elapsed,
            "rag_info": rag_info
        }

    except Exception as e:
        elapsed = round(time.time() - start_time, 2)
        return {
            "tester_name": "Tester",
            "query": "",
            "ai_response": f"AI Processing Error: {str(e)}",
            "elapsed_seconds": elapsed
        }

@router.post("/api/qa/report-issue")
async def qa_report_issue_api(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return {"error": "Unauthorized"}

    data = await request.json()
    tester_name = data.get("tester_name", "Tester")
    user_query = data.get("user_query", "")
    ai_response = data.get("ai_response", "")
    issue_category = data.get("issue_category", "Wrong Info")
    corrected_response = data.get("corrected_response", "")

    new_report = QAIssueReport(
        tester_name=tester_name,
        user_query=user_query,
        ai_response=ai_response,
        issue_category=issue_category,
        corrected_response=corrected_response,
        status="pending"
    )
    db.add(new_report)
    await db.commit()
    await db.refresh(new_report)

    await log_service.log("WARNING", "QA Issue Reported", f"Tester '{tester_name}' reported an issue [{issue_category}] on query: {user_query[:50]}")

    return {"status": "success", "report_id": new_report.id}

@router.post("/api/qa/learn-issue/{report_id}")
async def qa_learn_issue_api(report_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return {"error": "Unauthorized"}

    stmt = select(QAIssueReport).where(QAIssueReport.id == report_id)
    res = await db.execute(stmt)
    report = res.scalar_one_or_none()

    if not report:
        return {"error": "Report not found"}

    if not report.corrected_response:
        return {"error": "No corrected response provided to learn from!"}

    # Add to Knowledge Base
    kb_title = f"QA Correction ({report.issue_category}): {report.user_query[:40]}"
    kb_content = f"Question/Query: {report.user_query}\nCorrect Answer: {report.corrected_response}"
    
    new_kb = KnowledgeEntry(
        category="qa_correction",
        title=kb_title,
        content=kb_content,
        is_active=True
    )
    db.add(new_kb)
    report.status = "learned"
    await db.commit()

    await log_service.log("SUCCESS", "AI Learned Correction", f"AI learned correction for QA Report #{report_id}")

    return {"status": "success", "message": "Correction successfully converted to Knowledge Base entry for AI learning!"}


