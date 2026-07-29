import hashlib
import json
import re
from datetime import datetime, timedelta
import google.generativeai as genai
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models import KnowledgeEntry, BotSetting, CacheEntry, Appointment
from app.services.calendar_service import calendar_service
from app.helpers import get_bot_setting, convert_bn_to_en

DEFAULT_FALLBACK_MESSAGE = (
    "দুঃখিত, এই মুহূর্তে উত্তর তৈরিতে সামান্য সমস্যা হচ্ছে। "
    "খুব দ্রুত আমাদের একজন প্রতিনিধি আপনার সাথে যোগাযোগ করবেন।"
)

DEFAULT_SYSTEM_PROMPT = (
    "তুমি একজন প্রফেশনাল, অত্যন্ত বিনয়ী এবং সেলস-ফোকাসড AI কাস্টমার সাপোর্ট স্পেশালিস্ট। "
    "গ্রাহকদের সাথে অত্যন্ত আন্তরিকভাবে কথা বলো। নিচে প্রদান করা Business Knowledge Base অনুযায়ী "
    "গ্রাহকের প্রশ্নের এমনভাবে উত্তর দাও যাতে তারা আমাদের প্রোডাক্ট বা সার্ভিস কিনতে আগ্রহী হয়। "
    "উত্তরে কেবল ফিচার না বলে, এটি গ্রাহকের ব্যবসার কী কী সুবিধা দেবে (যেমন: সময় ও হিসাবের ভুল বাঁচানো) তা বুঝিয়ে বলো। "
    "প্রাসঙ্গিক হলে উত্তরের শেষে ভদ্রভাবে একটি প্রশ্ন বা কল-টু-অ্যাকশন রাখো (যেমন: 'আপনি কি আমাদের ফ্রি ডেমো দেখতে চান?')."
)

class AIService:
    def __init__(self):
        self._cached_api_key = None
        self._configured = False

    def _ensure_genai_configured(self, api_key: str):
        if api_key and (not self._configured or self._cached_api_key != api_key):
            genai.configure(api_key=api_key)
            self._cached_api_key = api_key
            self._configured = True

    async def get_fallback_message(self, db: AsyncSession) -> str:
        return await get_bot_setting(db, "fallback_message", DEFAULT_FALLBACK_MESSAGE)

    async def get_system_prompt(self, db: AsyncSession) -> str:
        return await get_bot_setting(db, "system_prompt", DEFAULT_SYSTEM_PROMPT)

    async def get_response_length(self, db: AsyncSession) -> str:
        val = await get_bot_setting(db, "response_length")
        return val if val else getattr(settings, "RESPONSE_LENGTH", "short")

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
        return {r.key: r.value for r in records}

    async def get_knowledge_base_data(self, db: AsyncSession) -> tuple[str, str]:
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)
        result = await db.execute(stmt)
        entries = result.scalars().all()
        
        if not entries:
            return "কোনো অতিরিক্ত তথ্য প্রদান করা হয়নি।", "empty_kb"
        
        kb_text = ""
        for entry in entries:
            kb_text += f"\n- [{entry.category.upper()}] {entry.title}: {entry.content}"
        
        kb_hash = hashlib.md5(kb_text.encode("utf-8")).hexdigest()
        return kb_text, kb_hash

    async def get_gemini_config(self, db: AsyncSession) -> tuple[str, str]:
        stmt = select(BotSetting).where(BotSetting.key.in_(["gemini_api_key", "gemini_model"]))
        result = await db.execute(stmt)
        records = result.scalars().all()
        settings_dict = {r.key: r.value for r in records}

        api_key = settings_dict.get("gemini_api_key", "")
        model_name = settings_dict.get("gemini_model", "gemini-2.0-flash")
        return api_key, model_name

    def get_available_models(self, api_key: str = None) -> list[dict]:
        try:
            key = api_key or settings.GEMINI_API_KEY
            if key and not key.startswith("your_"):
                self._ensure_genai_configured(key)
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
        val = await get_bot_setting(db, "booking_keywords")
        if val:
            return [k.strip().lower() for k in val.split(",") if k.strip()]
        return [
            "meeting", "appointment", "book", "schedule", "appoint",
            "মিটিং", "অ্যাপয়েন্টমেন্ট", "বুক", "শিডিউল", "দেখা", "কল", "ডেমো", "ট্রায়াল"
        ]

    async def get_detail_keywords(self, db: AsyncSession) -> list[str]:
        val = await get_bot_setting(db, "detail_keywords")
        if val:
            return [k.strip().lower() for k in val.split(",") if k.strip()]
        default_val = getattr(settings, "DETAIL_KEYWORDS", "")
        return [k.strip().lower() for k in default_val.split(",") if k.strip()]

    async def is_booking_intent(self, user_message: str, db: AsyncSession) -> bool:
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

        dynamic_digit_pattern = r'(\d{1,2}|[০-৯]{1,2})\s*(?:ta|tai|tar|টার|টা|pm|am|:\d\d|তারিখ|তারিখের)'
        if re.search(dynamic_digit_pattern, lowered):
            return True

        return False

    async def _handle_calendar_booking(self, user_message: str, calendar_config: dict, db: AsyncSession) -> str:
        phone_match = re.search(r'01[3-9]\d{8}', user_message)
        cust_phone = phone_match.group(0) if phone_match else ""
        msg_translated = convert_bn_to_en(user_message)
        now = datetime.now()

        if any(w in msg_translated.lower() for w in ["ajker", "ajke", "aj", "আজকের", "আজকে", "আজ"]):
            target_date = now
        elif any(w in msg_translated.lower() for w in ["kalke", "kal", "আগামীকাল", "কালকে"]):
            target_date = now + timedelta(days=1)
        else:
            target_date = now + timedelta(days=1)
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

        hour = 12
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
            return f"[SYSTEM ACTION: Requested slot on {final_date} at {formatted_time} was busy. Google Calendar booked next available slot for {final_date} at {final_time}. Clearly confirm this date ({final_date}) and time ({final_time}) to customer.]"
        return f"[SYSTEM ACTION: Google Calendar appointment successfully booked for {final_date} at {final_time}. Clearly confirm this exact date ({final_date}) and time ({final_time}) to customer.]"

    async def generate_response(self, user_message: str, history: list, db: AsyncSession) -> tuple[str, bool]:
        fallback_msg = await self.get_fallback_message(db)
        response_length = await self.get_response_length(db)
        normalized_query = user_message.strip().lower()
        is_booking_query = await self.is_booking_intent(normalized_query, db)

        knowledge_base, kb_hash = await self.get_knowledge_base_data(db)

        # Cache key includes Response Length + KB Hash + Query
        query_hash = hashlib.sha256(f"{response_length}:{kb_hash}:{normalized_query}".encode("utf-8")).hexdigest()

        if not is_booking_query:
            stmt_cache = select(CacheEntry).where(CacheEntry.prompt_hash == query_hash)
            cache_res = await db.execute(stmt_cache)
            cached_entry = cache_res.scalar_one_or_none()

            if cached_entry:
                if cached_entry.expires_at is None or cached_entry.expires_at > datetime.utcnow():
                    print(f"[CACHE HIT] Returning cached response for query: {user_message}")
                    return cached_entry.ai_response, True
                else:
                    await db.delete(cached_entry)
                    await db.commit()

        api_key, model_name = await self.get_gemini_config(db)

        if not api_key or api_key.startswith("your_") or api_key == "":
            print("[AI SERVICE WARNING] Gemini API key not configured. Using fallback message.")
            return fallback_msg, False

        try:
            self._ensure_genai_configured(api_key)

            calendar_booking_info = ""
            calendar_config = await self.get_calendar_config(db)
            if calendar_config.get("google_calendar_token") or calendar_config.get("google_refresh_token"):
                if is_booking_query:
                    calendar_booking_info = await self._handle_calendar_booking(user_message, calendar_config, db)

            system_prompt = await self.get_system_prompt(db)

            length_guides = {
                "short": "CRITICAL INSTRUCTION: Reply in maximum 1-2 VERY SHORT, DIRECT, and COMPLETE sentences in Bangla. Do NOT explain your rules, do NOT output word counts, do NOT output formatting thoughts, and do NOT include parentheses or meta-text. Just output the direct reply to the customer.",
                "medium": "IMPORTANT: Provide a clear and complete response within 2-3 concise sentences. Do NOT output word/sentence counts or meta-commentary. Just output the direct reply.",
                "long": "IMPORTANT: Provide a detailed and complete response."
            }
            # Dynamic token/length escalation based on user query intent
            active_length = response_length.lower()
            detail_keywords = await self.get_detail_keywords(db)
            
            # If the user asks a detailed question or their message is long, automatically escalate length constraints
            if active_length == "short":
                if any(kw in normalized_query for kw in detail_keywords) or len(normalized_query) > 80:
                    active_length = "medium"
            elif active_length == "medium":
                if any(kw in normalized_query for kw in detail_keywords) or len(normalized_query) > 150:
                    active_length = "long"

            length_guide = length_guides.get(active_length, length_guides["short"])
            max_tokens_map = {"short": 600, "medium": 1200, "long": 2000}
            max_tokens = max_tokens_map.get(active_length, 600)

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

            ai_text = ""
            try:
                ai_text = response.text.strip()
            except Exception:
                if response.candidates and response.candidates[0].content.parts:
                    ai_text = response.candidates[0].content.parts[0].text.strip()

            if not ai_text:
                ai_text = fallback_msg

            if not is_booking_query and ai_text:
                new_cache = CacheEntry(
                    prompt_hash=query_hash,
                    user_query=normalized_query,
                    ai_response=ai_text,
                    expires_at=datetime.utcnow() + timedelta(hours=24) # 24 Hours TTL
                )
                db.add(new_cache)
                await db.commit()

            return ai_text, False
        except Exception as e:
            print(f"[AI SERVICE ERROR] Failed to generate response: {e}")
            return fallback_msg, False

ai_service = AIService()
