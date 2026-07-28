import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "AI Chatbot Admin"
    DEBUG: bool = True
    
    # AI Settings
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    
    # Facebook Settings
    FB_PAGE_ACCESS_TOKEN: str = ""
    FB_VERIFY_TOKEN: str = ""
    FB_APP_SECRET: str = ""
    
    # WhatsApp Settings
    WA_ACCESS_TOKEN: str = ""
    WA_VERIFY_TOKEN: str = ""
    WA_PHONE_NUMBER_ID: str = ""
    
    # Admin Auth Settings
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    SECRET_KEY: str = "secret-key-12345"
    
    # Database Settings
    DATABASE_URL: str = "sqlite+aiosqlite:///./chatbot.db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
