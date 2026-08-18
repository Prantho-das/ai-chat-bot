import os
import base64
import math
from app.config import settings
from fastapi import APIRouter, Request, Response, Depends, Form, UploadFile, File, Query, status
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
from app.models import Conversation, Message, KnowledgeEntry, BotSetting, Appointment, SystemLog, Lead, QAIssueReport, Company, ChannelAccount
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
        
        # Ensure default company exists
        comp_stmt = select(Company).where(Company.id == 1)
        res = await db.execute(comp_stmt)
        if not res.scalar_one_or_none():
            default_comp = Company(
                id=1,
                name="Default Company",
                slug="default",
                description="Default Primary Company for bot operations",
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                ai_model="gemini-2.5-flash",
                temperature=0.7
            )
            db.add(default_comp)
            await db.commit()

        await log_service.log("INFO", "DB Migration", "Database schema migration executed successfully by Admin.")
        return RedirectResponse(url="/admin/settings?msg=migration_success", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        await log_service.log("ERROR", "DB Migration", f"Database migration failed: {e}")
        return RedirectResponse(url="/admin/settings?error=migration_failed", status_code=status.HTTP_303_SEE_OTHER)

COOKIE_NAME = "admin_token"
TOKEN_MAX_AGE = 8 * 3600 # 8 Hours

def get_current_user_context(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME) or request.cookies.get("admin_session")
    if not token:
        return None
    if token == "authenticated":
        return {"user": settings.ADMIN_USERNAME, "role": "super_admin", "company_id": None}
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired):
        return None

def is_authenticated(request: Request) -> bool:
    return get_current_user_context(request) is not None

async def render_admin_page(template_name: str, request: Request, db: AsyncSession, context: dict):
    user_ctx = get_current_user_context(request) or {}
    context["current_user"] = user_ctx
    context["is_super_admin"] = (user_ctx.get("role") == "super_admin")

    simplified_mode = await get_bot_setting(db, "simplified_client_mode", "false")
    context["simplified_client_mode"] = (simplified_mode == "true")

    # Fetch companies for global company switcher
    try:
        comps_res = await db.execute(select(Company).order_by(Company.id.asc()))
        all_companies = comps_res.scalars().all()
        if not all_companies:
            # Seed default company
            default_comp = Company(
                id=1,
                name="Default Company",
                slug="default",
                description="Default Primary Company",
                system_prompt=DEFAULT_SYSTEM_PROMPT
            )
            db.add(default_comp)
            await db.commit()
            all_companies = [default_comp]
        
        # If company user, lock to their company
        if user_ctx.get("role") == "company_user" and user_ctx.get("company_id"):
            user_comp_id = user_ctx.get("company_id")
            context["all_companies"] = [c for c in all_companies if c.id == user_comp_id]
            context["active_company_id"] = str(user_comp_id)
        else:
            context["all_companies"] = all_companies
            active_comp_id = request.cookies.get("active_company_id", "all")
            context["active_company_id"] = active_comp_id
    except Exception as e:
        context["all_companies"] = []
        context["active_company_id"] = "all"

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
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_db)):
    clean_username = username.strip()
    clean_password = password.strip()

    # 1. Master Super Admin Check (.env fallback)
    if clean_username == settings.ADMIN_USERNAME and clean_password == settings.ADMIN_PASSWORD:
        response = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        token = serializer.dumps({
            "user": clean_username,
            "role": "super_admin",
            "company_id": None
        })
        response.set_cookie(key=COOKIE_NAME, value=token, httponly=True, max_age=TOKEN_MAX_AGE)
        return response

    # 2. Database User Check (Master or Company Specific User)
    stmt = select(AdminUser).where(AdminUser.username == clean_username, AdminUser.is_active == True)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if user and user.password_hash == clean_password:
        response = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        token = serializer.dumps({
            "user": user.username,
            "role": user.role or "company_user",
            "company_id": user.company_id,
            "full_name": user.full_name or user.username
        })
        response.set_cookie(key=COOKIE_NAME, value=token, httponly=True, max_age=TOKEN_MAX_AGE)
        if user.company_id:
            response.set_cookie(key="active_company_id", value=str(user.company_id), max_age=TOKEN_MAX_AGE)
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

    user_ctx = get_current_user_context(request) or {}
    active_comp_id = request.cookies.get("active_company_id", "all")
    if user_ctx.get("role") == "company_user" and user_ctx.get("company_id"):
        target_company_id = user_ctx.get("company_id")
    elif active_comp_id and active_comp_id != "all":
        try:
            target_company_id = int(active_comp_id)
        except ValueError:
            target_company_id = None
    else:
        target_company_id = None

    conv_filter = [Conversation.company_id == target_company_id] if target_company_id else []
    kb_filter = [KnowledgeEntry.company_id == target_company_id] if target_company_id else []

    total_convs = await db.scalar(select(func.count(Conversation.id)).where(*conv_filter)) or 0
    total_msgs = await db.scalar(select(func.count(Message.id))) or 0
    total_kb = await db.scalar(select(func.count(KnowledgeEntry.id)).where(*kb_filter)) or 0
    cached_msgs = await db.scalar(select(func.count(Message.id)).where(Message.is_cached == True)) or 0
    
    # Fetch key reset timestamp
    reset_at_stmt = select(BotSetting.value).where(BotSetting.key == "current_key_reset_at")
    reset_at_str = await db.scalar(reset_at_stmt)
    reset_dt = None
    if reset_at_str:
        try:
            reset_dt = datetime.fromisoformat(reset_at_str)
        except Exception:
            reset_dt = None

    # Current Key Usage (Filtered by reset date if key changed)
    curr_filter = [Message.created_at >= reset_dt] if reset_dt else []
    curr_prompt_tokens = await db.scalar(select(func.sum(Message.prompt_tokens)).where(*curr_filter)) or 0
    curr_completion_tokens = await db.scalar(select(func.sum(Message.completion_tokens)).where(*curr_filter)) or 0
    curr_tokens_used = await db.scalar(select(func.sum(Message.total_tokens)).where(*curr_filter)) or 0

    # All-time Total Usage (Historical)
    total_prompt_tokens = await db.scalar(select(func.sum(Message.prompt_tokens))) or 0
    total_completion_tokens = await db.scalar(select(func.sum(Message.completion_tokens))) or 0
    total_tokens_used = await db.scalar(select(func.sum(Message.total_tokens))) or 0

    provider, api_key, model_name = await ai_service.get_ai_config(db)

    # Provider Specific Quota & Rate Limit Metadata
    provider_limits = {
        "gemini": {
            "name": "Google Gemini",
            "tier": "Free / Pay-as-you-go",
            "rpd": "1,500 Requests / Day",
            "tpm": "1,000,000 Tokens / Min",
            "rpm": "15 Requests / Min",
            "context_window": "1,048,576 Tokens (1M)",
            "pricing_input_1m": 0.10,
            "pricing_output_1m": 0.40
        },
        "openai": {
            "name": "OpenAI",
            "tier": "Usage-based Billing",
            "rpd": "Pay-as-you-go Balance",
            "tpm": "200,000 Tokens / Min",
            "rpm": "500 Requests / Min",
            "context_window": "128,000 Tokens",
            "pricing_input_1m": 0.15,
            "pricing_output_1m": 0.60
        },
        "deepseek": {
            "name": "DeepSeek",
            "tier": "Usage-based Billing",
            "rpd": "Account Credit Balance",
            "tpm": "100,000 Tokens / Min",
            "rpm": "60 Requests / Min",
            "context_window": "64,000 Tokens",
            "pricing_input_1m": 0.14,
            "pricing_output_1m": 0.28
        },
        "anthropic": {
            "name": "Anthropic Claude",
            "tier": "Usage-based Billing",
            "rpd": "Account Credit Balance",
            "tpm": "80,000 Tokens / Min",
            "rpm": "50 Requests / Min",
            "context_window": "200,000 Tokens",
            "pricing_input_1m": 3.00,
            "pricing_output_1m": 15.00
        }
    }

    current_p_info = provider_limits.get(provider, provider_limits["gemini"])

    # Dynamic pricing based on active provider
    input_rate = current_p_info["pricing_input_1m"] / 1000000.0
    output_rate = current_p_info["pricing_output_1m"] / 1000000.0
    curr_cost_usd = (curr_prompt_tokens * input_rate) + (curr_completion_tokens * output_rate)
    curr_cost_bdt = curr_cost_usd * 122.0

    alltime_cost_usd = (total_prompt_tokens * input_rate) + (total_completion_tokens * output_rate)
    alltime_cost_bdt = alltime_cost_usd * 122.0

    # Calculate Savings Percentage
    ai_msgs_total = await db.scalar(select(func.count(Message.id)).where(Message.role == "assistant")) or 0
    savings_pct = round((cached_msgs / ai_msgs_total * 100), 1) if ai_msgs_total > 0 else 0.0

    stmt = select(Conversation)
    if conv_filter:
        stmt = stmt.where(*conv_filter)
    stmt = stmt.order_by(Conversation.updated_at.desc()).limit(10)
    result = await db.execute(stmt)
    recent_conversations = result.scalars().all()

    stats = {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "knowledge_entries": total_kb,
        "cached_messages": cached_msgs,
        "curr_tokens_used": curr_tokens_used,
        "curr_prompt_tokens": curr_prompt_tokens,
        "curr_completion_tokens": curr_completion_tokens,
        "curr_cost_usd": f"{curr_cost_usd:.4f}",
        "curr_cost_bdt": f"{curr_cost_bdt:.2f}",
        "total_tokens_used": total_tokens_used,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "alltime_cost_usd": f"{alltime_cost_usd:.4f}",
        "alltime_cost_bdt": f"{alltime_cost_bdt:.2f}",
        "savings_pct": savings_pct,
        "active_provider": provider.upper(),
        "active_model": model_name,
        "provider_info": current_p_info,
        "has_api_key": bool(api_key and not api_key.startswith("your_")),
        "is_reset": bool(reset_dt)
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
    company_filter: str = None,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    active_comp_id = request.cookies.get("active_company_id", "all")
    selected_comp = company_filter or active_comp_id

    # Base query
    stmt = select(KnowledgeEntry)

    if selected_comp and selected_comp != "all":
        try:
            stmt = stmt.where(KnowledgeEntry.company_id == int(selected_comp))
        except ValueError:
            pass

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
    missing_embed_count = sum(1 for e in all_entries if not e.embedding_json or not e.embedding_json.strip() or e.embedding_json == "[]")

    return await render_admin_page("knowledge_base.html", request, db, {
        "entries": entries,
        "total_count": total_count,
        "active_count": active_count,
        "categories_count": len(categories_set),
        "missing_embed_count": missing_embed_count,
        "q": q or "",
        "category_filter": category or "all",
        "status_filter": status_filter or "all",
        "company_filter": selected_comp or "all",
        "saved": request.query_params.get("saved"),
        "reembedded": request.query_params.get("reembedded"),
        "error": request.query_params.get("error")
    })

@router.post("/knowledge/add")
async def add_knowledge(
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    is_active: bool = Form(False),
    company_id: int = Form(1),
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
        is_active=is_active,
        company_id=company_id
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


@router.post("/knowledge/generate-missing-embeddings")
async def generate_missing_embeddings(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(KnowledgeEntry).where(
        (KnowledgeEntry.embedding_json.is_(None)) |
        (KnowledgeEntry.embedding_json == "") |
        (KnowledgeEntry.embedding_json == "[]")
    )
    result = await db.execute(stmt)
    missing_entries = result.scalars().all()

    count = 0
    for entry in missing_entries:
        text_to_embed = f"{entry.title.strip()}\n{entry.content.strip()}"
        emb_json = await ai_service.generate_embedding(text_to_embed, db=db)
        if emb_json:
            entry.embedding_json = emb_json
            count += 1

    if count > 0:
        await db.commit()

    return RedirectResponse(url=f"/admin/knowledge?reembedded={count}", status_code=status.HTTP_302_FOUND)

@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(
    request: Request,
    platform: str = None,
    q: str = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Conversation)

    user_ctx = get_current_user_context(request) or {}
    active_comp_id = request.cookies.get("active_company_id", "all")
    if user_ctx.get("role") == "company_user" and user_ctx.get("company_id"):
        stmt = stmt.where(Conversation.company_id == user_ctx.get("company_id"))
    elif active_comp_id and active_comp_id != "all":
        try:
            stmt = stmt.where(Conversation.company_id == int(active_comp_id))
        except ValueError:
            pass

    if platform and platform != "all":
        stmt = stmt.where(Conversation.platform == platform)

    if q and q.strip():
        search_filter = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Conversation.sender_name.like(search_filter),
                Conversation.sender_id.like(search_filter)
            )
        )

    # Calculate count for pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_count = await db.scalar(count_stmt) or 0

    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    stmt = stmt.order_by(Conversation.updated_at.desc()).offset(offset).limit(per_page)
    result = await db.execute(stmt)
    conversations = result.scalars().all()

    return await render_admin_page("conversations.html", request, db, {
        "conversations": conversations,
        "selected_platform": platform or "all",
        "search_query": q or "",
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1
    })

@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(
    conv_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt_conv = select(Conversation).where(Conversation.id == conv_id)
    res_conv = await db.execute(stmt_conv)
    conv = res_conv.scalar_one_or_none()

    if not conv:
        return RedirectResponse(url="/admin/conversations", status_code=status.HTTP_302_FOUND)

    # Count total messages in this conversation
    count_stmt = select(func.count(Message.id)).where(Message.conversation_id == conv_id)
    total_messages = await db.scalar(count_stmt) or 0

    total_pages = math.ceil(total_messages / per_page) if total_messages > 0 else 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    stmt_msgs = (
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .offset(offset)
        .limit(per_page)
    )
    res_msgs = await db.execute(stmt_msgs)
    messages = res_msgs.scalars().all()

    return await render_admin_page("conversation_detail.html", request, db, {
        "conversation": conv,
        "messages": messages,
        "page": page,
        "per_page": per_page,
        "total_messages": total_messages,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1
    })

@router.post("/conversations/{conv_id}/toggle-status")
async def toggle_conversation_status(
    conv_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Conversation).where(Conversation.id == conv_id)
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    if conv:
        if conv.status == "active":
            conv.status = "paused" # Human agent active / AI Bot paused
        else:
            conv.status = "active" # AI Bot active
        await db.commit()

    return RedirectResponse(url=f"/admin/conversations/{conv_id}", status_code=status.HTTP_302_FOUND)

@router.post("/conversations/{conv_id}/send")
async def send_human_message(
    conv_id: int,
    request: Request,
    message_text: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Conversation).where(Conversation.id == conv_id)
    res = await db.execute(stmt)
    conv = res.scalar_one_or_none()
    
    if conv and message_text.strip():
        # Save message to DB
        msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=message_text.strip(),
            created_at=datetime.utcnow()
        )
        db.add(msg)
        conv.updated_at = datetime.utcnow()
        await db.commit()

        # Send live message to real user via Platform API
        try:
            if conv.platform == "messenger":
                from app.services.messenger_service import messenger_service
                await messenger_service.send_text_message(conv.sender_id, message_text.strip())
            elif conv.platform == "whatsapp":
                from app.services.whatsapp_service import whatsapp_service
                await whatsapp_service.send_text_message(conv.sender_id, message_text.strip())
            elif conv.platform == "instagram":
                from app.services.instagram_service import instagram_service
                await instagram_service.send_dm(conv.sender_id, message_text.strip())
        except Exception as e:
            logger.error(f"Failed to send live message to real user: {e}")

    return RedirectResponse(url=f"/admin/conversations/{conv_id}", status_code=status.HTTP_302_FOUND)

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(BotSetting)
    result = await db.execute(stmt)
    settings_records = result.scalars().all()
    settings_dict = {s.key: s.value for s in settings_records}

    creds = {
        "ai_provider": settings_dict.get("ai_provider", getattr(settings, "AI_PROVIDER", "gemini")),
        "gemini_api_key": settings_dict.get("gemini_api_key", settings.GEMINI_API_KEY),
        "gemini_model": settings_dict.get("gemini_model", settings.GEMINI_MODEL),
        "openai_api_key": settings_dict.get("openai_api_key", getattr(settings, "OPENAI_API_KEY", "")),
        "openai_model": settings_dict.get("openai_model", getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")),
        "deepseek_api_key": settings_dict.get("deepseek_api_key", getattr(settings, "DEEPSEEK_API_KEY", "")),
        "deepseek_model": settings_dict.get("deepseek_model", getattr(settings, "DEEPSEEK_MODEL", "deepseek-chat")),
        "anthropic_api_key": settings_dict.get("anthropic_api_key", getattr(settings, "ANTHROPIC_API_KEY", "")),
        "anthropic_model": settings_dict.get("anthropic_model", getattr(settings, "ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")),
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
        "gmail_app_password": settings_dict.get("gmail_app_password", getattr(settings, "GMAIL_APP_PASSWORD", "")),
        "enable_bot_name_rotation": settings_dict.get("enable_bot_name_rotation", "false"),
        "bot_names_list": settings_dict.get("bot_names_list", "PosTech, TechFlow, MarketAI, Assistant, DataBot")
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
    enable_bot_name_rotation: str = Form(None),
    bot_names_list: str = Form(None),
    booking_keywords: str = Form(None),
    detail_keywords: str = Form(None),
    ai_provider: str = Form(None),
    gemini_api_key: str = Form(None),
    gemini_model: str = Form(None),
    openai_api_key: str = Form(None),
    openai_model: str = Form(None),
    deepseek_api_key: str = Form(None),
    deepseek_model: str = Form(None),
    anthropic_api_key: str = Form(None),
    anthropic_model: str = Form(None),
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
        if bot_names_list is not None:
            await upsert_bot_setting(db, "bot_names_list", bot_names_list.strip())
        await upsert_bot_setting(db, "enable_bot_name_rotation", "true" if enable_bot_name_rotation == "true" else "false")
        await upsert_bot_setting(db, "simplified_client_mode", "true" if simplified_client_mode == "true" else "false")
        await db.commit()

    elif form_type == "credentials":
        raw_creds = {
            "ai_provider": ai_provider,
            "gemini_api_key": gemini_api_key,
            "gemini_model": gemini_model,
            "openai_api_key": openai_api_key,
            "openai_model": openai_model,
            "deepseek_api_key": deepseek_api_key,
            "deepseek_model": deepseek_model,
            "anthropic_api_key": anthropic_api_key,
            "anthropic_model": anthropic_model,
            "fb_page_access_token": fb_page_access_token,
            "fb_verify_token": fb_verify_token,
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
                # Check if API Key has changed to reset current usage
                if k in ["gemini_api_key", "openai_api_key", "deepseek_api_key", "anthropic_api_key", "ai_provider"]:
                    old_val_stmt = select(BotSetting.value).where(BotSetting.key == k)
                    old_val = await db.scalar(old_val_stmt)
                    if old_val and old_val.strip() != val:
                        # Key has changed! Record reset timestamp
                        now_str = datetime.utcnow().isoformat()
                        await upsert_bot_setting(db, "current_key_reset_at", now_str)

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

    user_ctx = get_current_user_context(request) or {}
    active_comp_id = request.cookies.get("active_company_id", "all")
    if user_ctx.get("role") == "company_user" and user_ctx.get("company_id"):
        target_company_id = user_ctx.get("company_id")
    elif active_comp_id and active_comp_id != "all":
        try:
            target_company_id = int(active_comp_id)
        except ValueError:
            target_company_id = None
    else:
        target_company_id = None

    stmt = select(Lead)
    if target_company_id:
        stmt = stmt.where(Lead.company_id == target_company_id)
    stmt = stmt.order_by(Lead.id.desc())
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
        image_b64 = data.get("image_base64")
        audio_b64 = data.get("audio_base64")

        if not user_query and not image_b64 and not audio_b64:
            return {"error": "Empty query and media"}

        image_bytes = None
        image_mime = None
        if image_b64:
            try:
                if "," in image_b64:
                    header, b64_str = image_b64.split(",", 1)
                    if "data:" in header and ";base64" in header:
                        image_mime = header.split("data:")[1].split(";")[0]
                else:
                    b64_str = image_b64
                    image_mime = "image/jpeg"
                image_bytes = base64.b64decode(b64_str)
            except Exception as img_err:
                print(f"[QA IMAGE DECODE ERROR] {img_err}")

        audio_bytes = None
        audio_mime = None
        if audio_b64:
            try:
                if "," in audio_b64:
                    header, b64_str = audio_b64.split(",", 1)
                    if "data:" in header and ";base64" in header:
                        audio_mime = header.split("data:")[1].split(";")[0]
                else:
                    b64_str = audio_b64
                    audio_mime = "audio/webm"
                audio_bytes = base64.b64decode(b64_str)
            except Exception as aud_err:
                print(f"[QA AUDIO DECODE ERROR] {aud_err}")

        ai_reply_data = await ai_service.generate_response(
            user_message=user_query,
            history=history,
            db=db,
            image_bytes=image_bytes,
            image_mime=image_mime,
            audio_bytes=audio_bytes,
            audio_mime=audio_mime,
            user_identifier=tester_name
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


# ==========================================
# MULTI-COMPANY & CHANNEL MANAGEMENT ROUTES
# ==========================================

@router.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_ctx = get_current_user_context(request)
    if not user_ctx or user_ctx.get("role") != "super_admin":
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

    comps_res = await db.execute(select(Company).order_by(Company.id.asc()))
    companies = comps_res.scalars().all()

    channels_res = await db.execute(select(ChannelAccount).order_by(ChannelAccount.id.desc()))
    channels = channels_res.scalars().all()

    users_res = await db.execute(select(AdminUser).where(AdminUser.role == "company_user").order_by(AdminUser.id.desc()))
    company_users = users_res.scalars().all()

    return await render_admin_page("companies.html", request, db, {
        "page_title": "Multi-Company & Channels",
        "companies": companies,
        "channels": channels,
        "company_users": company_users
    })

@router.post("/companies/switch")
async def switch_active_company(request: Request, company_id: str = Form(...)):
    user_ctx = get_current_user_context(request)
    if not user_ctx or user_ctx.get("role") != "super_admin":
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    referer = request.headers.get("referer", "/admin")
    response = RedirectResponse(url=referer, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="active_company_id", value=str(company_id), max_age=TOKEN_MAX_AGE)
    return response

@router.post("/companies/create")
async def create_company(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(""),
    ai_model: str = Form("gemini-2.5-flash"),
    temperature: float = Form(0.7),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    try:
        new_company = Company(
            name=name.strip(),
            slug=slug.strip().lower().replace(" ", "-"),
            description=description.strip(),
            system_prompt=system_prompt.strip() or DEFAULT_SYSTEM_PROMPT,
            ai_model=ai_model,
            temperature=temperature
        )
        db.add(new_company)
        await db.commit()
        await log_service.log("SUCCESS", "Company Created", f"Created company '{name}' with slug '{slug}'")
    except Exception as e:
        await log_service.log("ERROR", "Company Create Error", str(e))

    return RedirectResponse(url="/admin/companies?msg=company_created", status_code=status.HTTP_303_SEE_OTHER)

@router.post("/companies/edit/{company_id}")
async def edit_company(
    company_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(""),
    ai_model: str = Form("gemini-2.5-flash"),
    fallback_message: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    comp = await db.get(Company, company_id)
    if comp:
        comp.name = name.strip()
        comp.description = description.strip()
        comp.system_prompt = system_prompt.strip()
        comp.ai_model = ai_model
        comp.fallback_message = fallback_message.strip()
        await db.commit()
        await log_service.log("INFO", "Company Updated", f"Updated company ID #{company_id} ('{name}')")

    return RedirectResponse(url="/admin/companies?msg=company_updated", status_code=status.HTTP_303_SEE_OTHER)

@router.post("/channels/create")
async def create_channel_account(
    request: Request,
    company_id: int = Form(...),
    platform: str = Form(...),
    platform_account_id: str = Form(...),
    account_name: str = Form(""),
    access_token: str = Form(""),
    verify_token: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    try:
        new_channel = ChannelAccount(
            company_id=company_id,
            platform=platform,
            platform_account_id=platform_account_id.strip(),
            account_name=account_name.strip(),
            access_token=access_token.strip(),
            verify_token=verify_token.strip()
        )
        db.add(new_channel)
        await db.commit()
        await log_service.log("SUCCESS", "Channel Linked", f"Linked {platform} ID '{platform_account_id}' to Company #{company_id}")
    except Exception as e:
        await log_service.log("ERROR", "Channel Link Error", str(e))

    return RedirectResponse(url="/admin/companies?msg=channel_linked", status_code=status.HTTP_303_SEE_OTHER)

@router.post("/channels/delete/{channel_id}")
async def delete_channel_account(channel_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    channel = await db.get(ChannelAccount, channel_id)
    if channel:
        await db.delete(channel)
        await db.commit()
        await log_service.log("INFO", "Channel Removed", f"Removed channel account #{channel_id}")

    return RedirectResponse(url="/admin/companies?msg=channel_deleted", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/company-users/create")
async def create_company_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    company_id: int = Form(...),
    db: AsyncSession = Depends(get_db)
):
    user_ctx = get_current_user_context(request)
    if not user_ctx or user_ctx.get("role") != "super_admin":
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    try:
        new_user = AdminUser(
            username=username.strip().lower(),
            password_hash=password.strip(),
            full_name=full_name.strip(),
            role="company_user",
            company_id=company_id,
            is_active=True
        )
        db.add(new_user)
        await db.commit()
        await log_service.log("SUCCESS", "Company User Created", f"Created user '{username}' for Company #{company_id}")
    except Exception as e:
        await log_service.log("ERROR", "User Create Error", str(e))

    return RedirectResponse(url="/admin/companies?msg=user_created", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/company-users/delete/{user_id}")
async def delete_company_user(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_ctx = get_current_user_context(request)
    if not user_ctx or user_ctx.get("role") != "super_admin":
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    user = await db.get(AdminUser, user_id)
    if user:
        await db.delete(user)
        await db.commit()
        await log_service.log("INFO", "User Removed", f"Deleted user account #{user_id}")

    return RedirectResponse(url="/admin/companies?msg=user_deleted", status_code=status.HTTP_303_SEE_OTHER)




