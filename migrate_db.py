import asyncio
from app.database import engine
from sqlalchemy import text

async def add_column():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE cache_entries ADD COLUMN IF NOT EXISTS embedding_json TEXT;"))
    print("Column embedding_json added to cache_entries successfully.")

if __name__ == "__main__":
    asyncio.run(add_column())
