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

    # Mailchimp Settings
    MAILCHIMP_API_KEY: str = ""
    MAILCHIMP_LIST_ID: str = ""
    MAILCHIMP_SERVER_PREFIX: str = ""

    # Instagram Settings
    IG_ACCESS_TOKEN: str = ""
    IG_VERIFY_TOKEN: str = ""

    # Google Sheets Settings
    GOOGLE_SHEETS_SPREADSHEET_ID: str = ""

    # Push Notification Settings
    FCM_SERVER_KEY: str = ""
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_CLAIMS_EMAIL: str = "admin@example.com"

    # Gmail Settings
    GMAIL_SENDER_EMAIL: str = ""
    GMAIL_APP_PASSWORD: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()
