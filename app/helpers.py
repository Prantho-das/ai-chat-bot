from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import BotSetting

async def get_bot_setting(db: AsyncSession, key: str, default_val: str = "") -> str:
    stmt = select(BotSetting).where(BotSetting.key == key)
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    return setting.value if setting else default_val

async def upsert_bot_setting(db: AsyncSession, key: str, value: str):
    stmt = select(BotSetting).where(BotSetting.key == key)
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(BotSetting(key=key, value=value))

def convert_bn_to_en(text: str) -> str:
    bn_to_en_map = {'১': '1', '২': '2', '৩': '3', '৪': '4', '৫': '5', '৬': '6', '৭': '7', '৮': '8', '৯': '9', '০': '0'}
    for bn, en in bn_to_en_map.items():
        text = text.replace(bn, en)
    return text
