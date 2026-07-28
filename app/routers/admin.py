from fastapi import APIRouter, Request, Response, Depends, Form, status
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.config import settings
from app.models import Conversation, Message, KnowledgeEntry, BotSetting

router = APIRouter(prefix="/admin", tags=["Admin Panel"])
templates = Jinja2Templates(directory="app/templates")

def is_authenticated(request: Request) -> bool:
    return request.cookies.get("admin_session") == "authenticated"

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == settings.ADMIN_USERNAME and password == settings.ADMIN_PASSWORD:
        response = RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
        response.set_cookie(key="admin_session", value="authenticated", httponly=True)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("admin_session")
    return response

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    # Stats
    total_convs = await db.scalar(select(func.count(Conversation.id))) or 0
    total_msgs = await db.scalar(select(func.count(Message.id))) or 0
    total_kb = await db.scalar(select(func.count(KnowledgeEntry.id))) or 0
    cached_msgs = await db.scalar(select(func.count(Message.id)).where(Message.is_cached == True)) or 0

    # Recent conversations
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
        "entries": entries
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

    return RedirectResponse(url="/admin/knowledge", status_code=status.HTTP_302_FOUND)

from fastapi import UploadFile, File

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

    return RedirectResponse(url="/admin/knowledge", status_code=status.HTTP_302_FOUND)


@router.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    stmt = select(Conversation).order_by(Conversation.updated_at.desc())
    result = await db.execute(stmt)
    conversations = result.scalars().all()

    return templates.TemplateResponse("conversations.html", {
        "request": request,
        "conversations": conversations
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

    default_prompt = (
        "তুমি একজন পেশাদার এবং অত্যন্ত সহায়ক AI কাস্টমার সাপোর্ট এজেন্ট। "
        "নিচে প্রদান করা Business Knowledge Base অনুযায়ী গ্রাহকের প্রশ্নের সংক্ষেপে, "
        "সুন্দর ও মার্জিত ভাষায় বাংলা অথবা ইংরেজিতে উত্তর দাও।"
    )

    creds = {
        "gemini_api_key": settings_dict.get("gemini_api_key", settings.GEMINI_API_KEY),
        "fb_page_access_token": settings_dict.get("fb_page_access_token", settings.FB_PAGE_ACCESS_TOKEN),
        "fb_verify_token": settings_dict.get("fb_verify_token", settings.FB_VERIFY_TOKEN),
        "wa_access_token": settings_dict.get("wa_access_token", settings.WA_ACCESS_TOKEN),
        "wa_phone_number_id": settings_dict.get("wa_phone_number_id", settings.WA_PHONE_NUMBER_ID),
        "wa_verify_token": settings_dict.get("wa_verify_token", settings.WA_VERIFY_TOKEN),
        "google_calendar_token": settings_dict.get("google_calendar_token", ""),
    }

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "system_prompt": settings_dict.get("system_prompt", default_prompt),
        "creds": creds
    })

@router.post("/settings")
async def update_settings(
    request: Request,
    form_type: str = Form(...),
    system_prompt: str = Form(None),
    gemini_api_key: str = Form(None),
    fb_page_access_token: str = Form(None),
    fb_verify_token: str = Form(None),
    wa_access_token: str = Form(None),
    wa_phone_number_id: str = Form(None),
    wa_verify_token: str = Form(None),
    google_calendar_token: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    if form_type == "prompt" and system_prompt:
        stmt = select(BotSetting).where(BotSetting.key == "system_prompt")
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()
        if setting:
            setting.value = system_prompt
        else:
            db.add(BotSetting(key="system_prompt", value=system_prompt))
        await db.commit()

    elif form_type == "credentials":
        creds_to_update = {
            "gemini_api_key": gemini_api_key or "",
            "fb_page_access_token": fb_page_access_token or "",
            "fb_verify_token": fb_verify_token or "",
            "wa_access_token": wa_access_token or "",
            "wa_phone_number_id": wa_phone_number_id or "",
            "wa_verify_token": wa_verify_token or "",
        }
        for k, v in creds_to_update.items():
            stmt = select(BotSetting).where(BotSetting.key == k)
            res = await db.execute(stmt)
            setting = res.scalar_one_or_none()
            if setting:
                setting.value = v
            else:
                db.add(BotSetting(key=k, value=v))
            if hasattr(settings, k.upper()):
                setattr(settings, k.upper(), v)
        await db.commit()

    elif form_type == "calendar" and google_calendar_token is not None:
        stmt = select(BotSetting).where(BotSetting.key == "google_calendar_token")
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()
        if setting:
            setting.value = google_calendar_token
        else:
            db.add(BotSetting(key="google_calendar_token", value=google_calendar_token))
        await db.commit()

    return RedirectResponse(url="/admin/settings", status_code=status.HTTP_302_FOUND)




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
