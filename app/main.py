from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import engine, Base
from app.routers import webhook_messenger, webhook_whatsapp, admin

app = FastAPI(
    title=settings.APP_NAME,
    description="Facebook Messenger + WhatsApp AI Chatbot with Built-in Admin Dashboard",
    version="1.0.0"
)

# Include Routers
app.include_router(webhook_messenger.router)
app.include_router(webhook_whatsapp.router)
app.include_router(admin.router)

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

