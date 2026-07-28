import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "AI Chatbot Admin"
    DEBUG: bool = True
    
    # AI Settings
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    RESPONSE_LENGTH: str = "short" # short, medium, long
    
    # Facebook Settings
    FB_PAGE_ACCESS_TOKEN: str = ""
    FB_VERIFY_TOKEN: str = ""
    FB_APP_SECRET: str = ""
    
    # WhatsApp Settings
    WA_ACCESS_TOKEN: str = ""
    WA_VERIFY_TOKEN: str = ""
    WA_PHONE_NUMBER_ID: str = ""
    
    # Google Calendar Settings
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""
    GOOGLE_CALENDAR_ID: str = "primary"
    
    # Admin Auth Settings
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    SECRET_KEY: str = "secret-key-12345"
    
    # Database Settings (MySQL with aiomysql driver)
    DATABASE_URL: str = "mysql+aiomysql://root:password@localhost:3306/chatbot_db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()
