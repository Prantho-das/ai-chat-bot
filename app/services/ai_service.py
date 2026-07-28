import hashlib
import json
import re
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

    async def get_booking_keywords(self, db: AsyncSession) -> list[str]:
        stmt = select(BotSetting).where(BotSetting.key == "booking_keywords")
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()
        if setting and setting.value:
            return [k.strip().lower() for k in setting.value.split(",") if k.strip()]
        return [
            "meeting", "appointment", "book", "schedule", "appoint",
            "মিটিং", "অ্যাপয়েন্টমেন্ট", "বুক", "শিডিউল", "দেখা", "কল", "ডেমো", "ট্রায়াল"
        ]

    async def is_booking_intent(self, user_message: str, db: AsyncSession) -> bool:
        """Dynamic detection of appointment, meeting, date & time intents using DB keywords & Smart Patterns."""
        lowered = user_message.strip().lower()

        booking_keywords = await self.get_booking_keywords(db)
        if any(k in lowered for k in booking_keywords):
            return True

        date_words = [
            "today", "tomorrow", "ajker", "ajke", "aj", "kalke", "kal", "tarikh", "tariker",
            "আজকের", "আজকে", "আজ", "কালকে", "কাল", "আগামীকাল", "পরশু", "তারিখ", "তারিখের"
        ]
        if any(d in lowered for d in date_words):
            return True

        # Dynamic regex for any digit (Bangla or English) followed by time/date indicators
        dynamic_digit_pattern = r'(\d{1,2}|[০-৯]{1,2})\s*(?:ta|tai|tar|টার|টা|pm|am|:\d\d|তারিখ|তারিখের)'
        if re.search(dynamic_digit_pattern, lowered):
            return True

        return False

    async def generate_response(self, user_message: str, history: list, db: AsyncSession) -> tuple[str, bool]:
        response_length = await self.get_response_length(db)
        normalized_query = user_message.strip().lower()

        # Dynamic appointment/booking detection with DB custom keywords
        is_booking_query = await self.is_booking_intent(normalized_query, db)

        query_hash = hashlib.sha256(f"{response_length}:{normalized_query}".encode("utf-8")).hexdigest()
        if not is_booking_query:
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

            calendar_booking_info = ""
            calendar_config = await self.get_calendar_config(db)
            if calendar_config.get("google_calendar_token") or calendar_config.get("google_refresh_token"):
                if is_booking_query:
                    phone_match = re.search(r'01[3-9]\d{8}', user_message)
                    cust_phone = phone_match.group(0) if phone_match else ""

                    # Translate Bengali numbers to English
                    bn_to_en = {'১': '1', '২': '2', '৩': '3', '৪': '4', '৫': '5', '৬': '6', '৭': '7', '৮': '8', '৯': '9', '০': '0'}
                    msg_translated = user_message
                    for bn, en in bn_to_en.items():
                        msg_translated = msg_translated.replace(bn, en)

                    now = datetime.now()
                    
                    # Detect if Today or Tomorrow or specific day dynamically
                    if any(w in msg_translated.lower() for w in ["ajker", "ajke", "aj", "আজকের", "আজকে", "আজ"]):
                        target_date = now
                    elif any(w in msg_translated.lower() for w in ["kalke", "kal", "আগামীকাল", "কালকে"]):
                        target_date = now + timedelta(days=1)
                    else:
                        target_date = now + timedelta(days=1)
                        # Explicit dynamic date match (e.g. 1 to 31)
                        day_match = re.search(r'\b(3[01]|[12]\d|[1-9])\s*(?:tarikh|tariker|তারিখ|তারিখের)?\b', msg_translated.lower())
                        if day_match:
                            parsed_day = int(day_match.group(1))
                            if 1 <= parsed_day <= 31:
                                target_month = now.month
                                target_year = now.year
                                if parsed_day < now.day:
                                    target_month = target_month + 1 if target_month < 12 else 1
                                    if target_month == 1:
                                        target_year += 1
                                try:
                                    target_date = datetime(target_year, target_month, parsed_day)
                                except ValueError:
                                    pass

                    # Extract hour dynamically
                    hour = 12 # default 12 PM
                    hour_match = re.search(r'\b(1[0-2]|[1-9])\s*(?:ta|tai|tar|টার|টা|pm|am|:\d\d)?\b', msg_translated.lower())
                    if hour_match:
                        parsed_h = int(hour_match.group(1))
                        if parsed_h == 12:
                            hour = 12
                        elif parsed_h in [1, 2, 3, 4, 5, 6, 7]:
                            hour = parsed_h + 12
                        else:
                            hour = parsed_h

                    booking_dt = target_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                    start_iso = booking_dt.isoformat()
                    formatted_time = booking_dt.strftime('%I:%M %p')
                    formatted_date = booking_dt.strftime('%d %B, %Y')
                    booking_time_str = booking_dt.strftime('%Y-%m-%d %H:%M')

                    cal_res = await calendar_service.create_event(
                        calendar_config=calendar_config,
                        summary=f"Meeting with {cust_phone or 'Customer'}",
                        description=f"Automated Booking via AI Chatbot.\nCustomer Phone: {cust_phone}\nQuery: {user_message}",
                        start_time_iso=start_iso,
                        duration_minutes=30
                    )

                    final_time = cal_res.get("formatted_time", formatted_time)
                    final_date = cal_res.get("formatted_date", formatted_date)
                    is_rescheduled = cal_res.get("is_rescheduled", False)

                    from app.models import Appointment
                    new_appointment = Appointment(
                        customer_name="Customer",
                        customer_phone=cust_phone,
                        summary=f"Meeting ({final_date} at {final_time}): {user_message[:40]}",
                        booking_time=cal_res.get("start_time", booking_time_str),
                        google_event_link=cal_res.get('html_link', ''),
                        status="confirmed" if cal_res.get("success") else "pending"
                    )
                    db.add(new_appointment)
                    await db.commit()

                    if is_rescheduled:
                        calendar_booking_info = f"[SYSTEM ACTION: Requested slot on {final_date} at {formatted_time} was busy. Google Calendar booked next available slot for {final_date} at {final_time}. Clearly confirm this date ({final_date}) and time ({final_time}) to customer.]"
                    else:
                        calendar_booking_info = f"[SYSTEM ACTION: Google Calendar appointment successfully booked for {final_date} at {final_time}. Clearly confirm this exact date ({final_date}) and time ({final_time}) to customer.]"

            system_prompt = await self.get_system_prompt(db)
            knowledge_base = await self.get_knowledge_base(db)

            length_guides = {
                "short": "CRITICAL INSTRUCTION: Reply in maximum 1-2 VERY SHORT, DIRECT sentences in Bangla. Do NOT add filler text, unnecessary polite offers, or repeated marketing lines.",
                "medium": "IMPORTANT: Provide a clear response within 2-3 concise sentences.",
                "long": "IMPORTANT: Provide a detailed and complete response."
            }
            length_guide = length_guides.get(response_length.lower(), length_guides["short"])

            max_tokens_map = {
                "short": 300,
                "medium": 600,
                "long": 1200
            }
            max_tokens = max_tokens_map.get(response_length.lower(), 300)

            formatted_history = ""
            for msg in history[-3:]:
                role = "Customer" if msg.role == "user" else "Support AI"
                formatted_history += f"{role}: {msg.content}\n"

            full_prompt = f"""{system_prompt}
{length_guide}

[KNOWLEDGE BASE]
{knowledge_base}

{calendar_booking_info}

[RECENT HISTORY]
{formatted_history}

Customer: {user_message}
Support AI Reply:"""

            generation_config = genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=0.2
            )

            model = genai.GenerativeModel(model_name, generation_config=generation_config)
            response = model.generate_content(full_prompt)
            
            try:
                ai_text = response.text.strip()
            except Exception:
                if response.candidates and response.candidates[0].content.parts:
                    ai_text = response.candidates[0].content.parts[0].text.strip()
                else:
                    ai_text = "ধন্যবাদ! আপনার অ্যাপয়েন্টমেন্টটি সঠিকভাবে রেকর্ড করা হয়েছে।"

            if not is_booking_query:
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
            return "ধন্যবাদ আপনার বার্তার জন্য! আপনার অ্যাপয়েন্টমেন্ট সংক্রান্ত তথ্য সফলভাবে নোট করা হয়েছে।", False


ai_service = AIService()
