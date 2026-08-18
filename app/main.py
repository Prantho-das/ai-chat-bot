import socket
_orig_gai = socket.getaddrinfo
def _patched_gai(*args, **kwargs):
    return [r for r in _orig_gai(*args, **kwargs) if r[0] == socket.AF_INET]
socket.getaddrinfo = _patched_gai

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
import os

from app.config import settings
from app.database import engine, Base
from app.routers import webhook_messenger, webhook_whatsapp, webhook_instagram, api_marketing, admin, outreach

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Auto-seed default company
    try:
        from app.database import AsyncSessionLocal
        from app.models import Company
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            comp_res = await db.execute(select(Company).where(Company.id == 1))
            if not comp_res.scalar_one_or_none():
                default_comp = Company(
                    id=1,
                    name="Default Company",
                    slug="default",
                    description="Primary default company for bot operations",
                    system_prompt="তুমি প্রফেশনাল AI সাপোর্ট।",
                    ai_model="gemini-2.5-flash",
                    temperature=0.7
                )
                db.add(default_comp)
                await db.commit()
    except Exception as e:
        print(f"[STARTUP SEED INFO] {e}")

    yield

app = FastAPI(
    title=settings.APP_NAME,
    description="Facebook Messenger + WhatsApp AI Chatbot with Built-in Admin Dashboard",
    version="1.0.0",
    lifespan=lifespan
)

# Include Routers
app.include_router(webhook_messenger.router)
app.include_router(webhook_whatsapp.router)
app.include_router(webhook_instagram.router)
app.include_router(api_marketing.router)
app.include_router(admin.router)
app.include_router(outreach.router)

if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

from fastapi.responses import Response
from app.database import get_db
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.fb_catalog_service import fb_catalog_service

@app.get("/catalog/feed.xml")
async def catalog_feed_xml(request: Request, db: AsyncSession = Depends(get_db)):
    host_url = str(request.base_url).rstrip('/')
    xml_data = await fb_catalog_service.get_catalog_feed_xml(db, host_url)
    return Response(content=xml_data, media_type="application/xml")


