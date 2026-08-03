import socket
_orig_gai = socket.getaddrinfo
def _patched_gai(*args, **kwargs):
    return [r for r in _orig_gai(*args, **kwargs) if r[0] == socket.AF_INET]
socket.getaddrinfo = _patched_gai

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
import os

from app.config import settings
from app.database import engine, Base, init_db
from app.routers import webhook_messenger, webhook_whatsapp, webhook_instagram, api_marketing, admin

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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

if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

