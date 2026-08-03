from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

db_url = settings.DATABASE_URL
if "mysql" in db_url:
    try:
        import aiomysql
    except ImportError:
        db_url = "sqlite+aiosqlite:///./chatbot.db"
elif "postgres" in db_url:
    try:
        import asyncpg
    except ImportError:
        db_url = "sqlite+aiosqlite:///./chatbot.db"

engine = create_async_engine(db_url, echo=settings.DEBUG, future=True)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE cache_entries ADD COLUMN IF NOT EXISTS embedding_json TEXT;"))
        except Exception:
            pass
