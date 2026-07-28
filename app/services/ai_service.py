import hashlib
import google.generativeai as genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models import KnowledgeEntry, BotSetting, CacheEntry


class AIService:
    def __init__(self):
        if settings.GEMINI_API_KEY:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
        else:
            self.model = None

    async def get_system_prompt(self, db: AsyncSession) -> str:
        stmt = select(BotSetting).where(BotSetting.key == "system_prompt")
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()
        
        default_prompt = (
            "তুমি একজন পেশাদার এবং অত্যন্ত সহায়ক AI কাস্টমার সাপোর্ট এজেন্ট। "
            "নিচে প্রদান করা Business Knowledge Base অনুযায়ী গ্রাহকের প্রশ্নের সংক্ষেপে, "
            "সুন্দর ও মার্জিত ভাষায় বাংলা অথবা ইংরেজিতে উত্তর দাও।"
        )
        return setting.value if setting else default_prompt

    async def get_knowledge_base(self, db: AsyncSession) -> str:
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)
        result = await db.execute(stmt)
        entries = result.scalars().all()
        
        if not entries:
            return "কোনো অতিরিক্ত তথ্য প্রদান করা হয়নি।"
        
        kb_text = ""
        for entry in entries:
            kb_text += f"\n- [{entry.category.upper()}] {entry.title}: {entry.content}"
        return kb_text

    async def generate_response(self, user_message: str, history: list, db: AsyncSession) -> tuple[str, bool]:
        clean_query = user_message.strip().lower()
        query_hash = hashlib.sha256(clean_query.encode("utf-8")).hexdigest()

        # Check Cache to save API Tokens
        stmt_cache = select(CacheEntry).where(CacheEntry.prompt_hash == query_hash)
        cache_res = await db.execute(stmt_cache)
        cached_entry = cache_res.scalar_one_or_none()

        if cached_entry:
            print(f"[CACHE HIT] Returning cached response for query: {user_message}")
            return cached_entry.ai_response, True

        if not self.model:
            return "ধন্যবাদ আপনার বার্তার জন্য! আমাদের এআই সার্ভিসটি বর্তমানে সেটআপ প্রক্রিয়াধীন রয়েছে।", False

        try:
            system_prompt = await self.get_system_prompt(db)
            knowledge_base = await self.get_knowledge_base(db)

            formatted_history = ""
            for msg in history[-5:]:
                role = "Customer" if msg.role == "user" else "Support AI"
                formatted_history += f"{role}: {msg.content}\n"

            full_prompt = f"""
{system_prompt}

[BUSINESS KNOWLEDGE BASE]
{knowledge_base}

[RECENT CONVERSATION HISTORY]
{formatted_history}

[CURRENT USER MESSAGE]
Customer: {user_message}

Support AI Reply:
"""
            response = self.model.generate_content(full_prompt)
            ai_text = response.text.strip()

            # Save in cache for future identical queries
            new_cache = CacheEntry(
                prompt_hash=query_hash,
                user_query=clean_query,
                ai_response=ai_text
            )
            db.add(new_cache)
            await db.commit()

            return ai_text, False
        except Exception as e:
            print(f"Error generating AI response: {e}")
            return "দুঃখিত, এই মুহূর্তে উত্তর তৈরিতে সামান্য সমস্যা হচ্ছে। খুব দ্রুত আমাদের এজেন্ট আপনার সাথে যোগাযোগ করবে।", False


ai_service = AIService()

