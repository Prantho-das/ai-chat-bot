import hashlib
import json
from datetime import datetime, timedelta
import google.generativeai as genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models import KnowledgeEntry, BotSetting, CacheEntry
from app.services.calendar_service import calendar_service

class AIService:
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

    async def get_response_length(self, db: AsyncSession) -> str:
        stmt = select(BotSetting).where(BotSetting.key == "response_length")
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()
        return setting.value if setting else getattr(settings, "RESPONSE_LENGTH", "short")

    async def get_calendar_config(self, db: AsyncSession) -> dict:
        keys = [
            "google_calendar_token",
            "google_client_id",
            "google_client_secret",
            "google_refresh_token",
            "google_calendar_id"
        ]
        stmt = select(BotSetting).where(BotSetting.key.in_(keys))
        result = await db.execute(stmt)
        records = result.scalars().all()
        config = {r.key: r.value for r in records}
        return config

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

    async def get_gemini_config(self, db: AsyncSession) -> tuple[str, str]:
        stmt = select(BotSetting).where(BotSetting.key.in_(["gemini_api_key", "gemini_model"]))
        result = await db.execute(stmt)
        records = result.scalars().all()
        settings_dict = {r.key: r.value for r in records}

        api_key = settings_dict.get("gemini_api_key") or settings.GEMINI_API_KEY
        model_name = settings_dict.get("gemini_model") or getattr(settings, "GEMINI_MODEL", "gemini-2.0-flash")
        return api_key, model_name

    def get_available_models(self, api_key: str = None) -> list[dict]:
        try:
            key = api_key or settings.GEMINI_API_KEY
            if key and not key.startswith("your_"):
                genai.configure(api_key=key)
                models = []
                for m in genai.list_models():
                    if "generateContent" in m.supported_generation_methods:
                        model_id = m.name.replace("models/", "")
                        models.append({
                            "id": model_id,
                            "name": m.display_name or model_id
                        })
                if models:
                    return models
        except Exception as e:
            print(f"Error fetching models from Gemini API: {e}")

        return [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
            {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
            {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro"},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        ]

    async def generate_response(self, user_message: str, history: list, db: AsyncSession) -> tuple[str, bool]:
        response_length = await self.get_response_length(db)
        normalized_query = user_message.strip().lower()
        query_hash = hashlib.sha256(f"{response_length}:{normalized_query}".encode("utf-8")).hexdigest()

        # Check Cache first to save API Tokens
        stmt_cache = select(CacheEntry).where(CacheEntry.prompt_hash == query_hash)
        cache_res = await db.execute(stmt_cache)
        cached_entry = cache_res.scalar_one_or_none()

        if cached_entry:
            print(f"[CACHE HIT] Returning cached response for query: {user_message}")
            return cached_entry.ai_response, True

        api_key, model_name = await self.get_gemini_config(db)

        if not api_key or api_key.startswith("your_") or api_key == "":
            return "ধন্যবাদ আপনার বার্তার জন্য! আমাদের এআই সার্ভিসটির Gemini API Key সেটআপ করা হয়নি। দয়া করে এডমিন প্যানেল থেকে Gemini API Key যুক্ত করুন।", False

        try:
            genai.configure(api_key=api_key)

            max_tokens_map = {
                "short": 150,
                "medium": 350,
                "long": 800
            }
            max_tokens = max_tokens_map.get(response_length.lower(), 150)

            generation_config = genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=0.3
            )

            model = genai.GenerativeModel(model_name, generation_config=generation_config)

            system_prompt = await self.get_system_prompt(db)
            knowledge_base = await self.get_knowledge_base(db)

            length_instructions = {
                "short": "[RESPONSE LENGTH INSTRUCTION: Keep response very SHORT, crisp, and concise (within 2-3 sentences max).]",
                "medium": "[RESPONSE LENGTH INSTRUCTION: Provide a moderate/medium length response with clear details.]",
                "long": "[RESPONSE LENGTH INSTRUCTION: Provide a comprehensive and detailed response explaining everything thoroughly.]"
            }
            length_guide = length_instructions.get(response_length.lower(), length_instructions["short"])

            formatted_history = ""
            for msg in history[-3:]:
                role = "Customer" if msg.role == "user" else "Support AI"
                formatted_history += f"{role}: {msg.content}\n"

            full_prompt = f"""{system_prompt}
{length_guide}

[KNOWLEDGE BASE]
{knowledge_base}

[HISTORY]
{formatted_history}
Customer: {user_message}
Support AI Reply:"""

            response = model.generate_content(full_prompt)
            ai_text = response.text.strip()

            # Automatic Google Calendar Event Creation Trigger
            calendar_config = await self.get_calendar_config(db)
            if calendar_config.get("google_calendar_token") or calendar_config.get("google_refresh_token"):
                if any(k in user_message.lower() for k in ["meeting", "appointment", "book", "schedule", "মিটিং", "অ্যাপয়েন্টমেন্ট", "বুক", "কালকে", "আগামীকাল", "সময়", "টাই"]):
                    import re
                    # Extract phone number if present
                    phone_match = re.search(r'01[3-9]\d{8}', user_message)
                    cust_phone = phone_match.group(0) if phone_match else ""

                    # Extract hour if mentioned (e.g. 12, 2, 10, 3, ১২, ২)
                    hour = 14 # default 2 PM
                    bn_to_en = {'১': '1', '২': '2', '৩': '3', '৪': '4', '৫': '5', '৬': '6', '৭': '7', '৮': '8', '৯': '9', '০': '0'}
                    msg_translated = user_message
                    for bn, en in bn_to_en.items():
                        msg_translated = msg_translated.replace(bn, en)

                    hour_match = re.search(r'\b(1[0-2]|[1-9])\s*(?:ta|tai|টার|টা|pm|am|:\d\d)?\b', msg_translated.lower())
                    if hour_match:
                        parsed_h = int(hour_match.group(1))
                        if parsed_h in [1, 2, 3, 4, 5, 6, 7]:
                            hour = parsed_h + 12 # Convert PM hours (1-7 PM)
                        else:
                            hour = parsed_h

                    booking_dt = (datetime.now() + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
                    start_iso = booking_dt.isoformat()
                    booking_time_str = booking_dt.strftime('%Y-%m-%d %H:%M')
                    
                    cal_res = await calendar_service.create_event(
                        calendar_config=calendar_config,
                        summary=f"Meeting with {cust_phone or 'Customer'}",
                        description=f"Automated Booking via AI Chatbot.\nCustomer Phone: {cust_phone}\nQuery: {user_message}",
                        start_time_iso=start_iso,
                        duration_minutes=30
                    )
                    
                    # Save to DB appointments table
                    from app.models import Appointment
                    new_appointment = Appointment(
                        customer_name="Customer",
                        customer_phone=cust_phone,
                        summary=f"Meeting ({booking_dt.strftime('%I:%M %p')}): {user_message[:40]}",
                        booking_time=booking_time_str,
                        google_event_link=cal_res.get('html_link', ''),
                        status="confirmed" if cal_res.get("success") else "pending"
                    )
                    db.add(new_appointment)
                    await db.commit()

                    if cal_res.get("success"):
                        print(f"[GOOGLE CALENDAR SUCCESS] Event Created: {cal_res.get('html_link')}")

            # Save in cache for future identical queries
            new_cache = CacheEntry(
                prompt_hash=query_hash,
                user_query=normalized_query,
                ai_response=ai_text
            )
            db.add(new_cache)
            await db.commit()

            return ai_text, False
        except Exception as e:
            print(f"Error generating AI response: {e}")
            return "দুঃখিত, এই মুহূর্তে উত্তর তৈরিতে সামান্য সমস্যা হচ্ছে। খুব দ্রুত আমাদের এজেন্ট আপনার সাথে যোগাযোগ করবে।", False


ai_service = AIService()
