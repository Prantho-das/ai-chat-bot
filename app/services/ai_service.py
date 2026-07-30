import hashlib
import json
import re
from datetime import datetime, timedelta
try:
    import google.generativeai as genai
except ImportError:
    genai = None
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
    "তুমি একজন বিশ্বস্ত, আন্তরিক এবং সেলস-ফোকাসড AI কাস্টমার সাপোর্ট এজেন্ট। "
    "তোমার কথা বলার ধরন হবে একদম মানুষের মতো — বন্ধুসুলভ, সহজ ভাষায়, এবং কৃত্রিম বা রোবোটিক নয়।\n\n"

    "## মূল নিয়মাবলী:\n"
    "1. **শুধুমাত্র Knowledge Base থেকে উত্তর দাও।** Knowledge Base-এ নেই এমন কিছু কখনো বানিয়ে বলবে না। "
    "যদি উত্তর জানা না থাকে, সৎভাবে বলো: 'এই বিষয়ে আমাদের টিমের সাথে সরাসরি কথা বলতে পারলে আরো ভালো হবে।'\n"
    "2. **হ্যালুসিনেশন করবে না।** মিথ্যা তথ্য, মনগড়া দাম, ফিচার বা অফার বলবে না। নিশ্চিত না হলে বলো না।\n"
    "3. **অশ্লীল, অপমানজনক বা অপ্রাসঙ্গিক মেসেজ পেলে:** শান্তভাবে এবং পেশাদারভাবে বিষয়টি এড়িয়ে যাও। "
    "রাগ করবে না, তর্ক করবে না। বলো: 'আমি আপনাকে আমাদের প্রোডাক্ট ও সার্ভিস নিয়ে সাহায্য করতে পারি। কিছু জানতে চাইলে বলুন!' "
    "কোনো অবস্থাতেই অশ্লীল বা অপ্রাসঙ্গিক কথায় সাড়া দেবে না বা নিজে এ ধরনের কিছু বলবে না।\n"
    "4. **Convincing Sales Approach:** গ্রাহকের সমস্যা বা চাহিদা বুঝে সেই অনুযায়ী প্রোডাক্ট/সার্ভিসের সুবিধা তুলে ধরো। "
    "শুধু ফিচার তালিকা না দিয়ে, কীভাবে এটি তাদের ব্যবসা বা জীবনে কাজে লাগবে তা বুঝিয়ে বলো।\n"
    "5. **প্রাসঙ্গিক হলে** উত্তরের শেষে একটি স্বাভাবিক প্রশ্ন বা কল-টু-অ্যাকশন রাখো (যেমন: 'আপনি কি ডেমো দেখতে চান?')।\n"
    "6. **সম্পূর্ণ বাক্যে উত্তর দাও।** কোনো উত্তর মাঝখানে থামিয়ে দেবে না বা অসম্পূর্ণ রাখবে না।\n"
    "7. **মেটা-টেক্সট দেবে না।** নিজের নিয়ম, word count, বা চিন্তা প্রক্রিয়া আউটপুটে দেখাবে না। শুধু গ্রাহকের জন্য সরাসরি উত্তর দাও।"
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

139:     async def is_booking_intent(self, user_message: str, db: AsyncSession) -> bool:
140:         lowered = user_message.strip().lower()
141: 
142:         # 1. If user is asking a general question/query about booking availability (e.g. "kora jabe ki?", "hobe ki?", "kivabe korbo?")
143:         question_indicators = ["kora jabe", "hobe ki", "jabe ki", "jabe?", "hobe?", "kivabe", "pari ki", "করা যাবে", "হবে কি", "যাবে কি", "কীভাবে", "পারি কি"]
144:         has_question = any(q in lowered for q in question_indicators)
145: 
146:         # Explicit phone number or clear time specification indicates actual confirmation intent
147:         has_phone = bool(re.search(r'01[3-9]\d{8}', lowered))
148:         has_explicit_time = bool(re.search(r'\b(\d{1,2}|[০-৯]{1,2})\s*(?:ta|tai|tar|টার|টা|pm|am|:\d\d)\b', lowered))
149: 
150:         # Direct booking command words
151:         direct_commands = ["book koro", "fix koro", "book korin", "booking den", "book den", "বুক করুন", "বুক করে দেন", "মিটিং ফিক্স করুন"]
152:         has_direct_command = any(cmd in lowered for cmd in direct_commands)
153: 
154:         # If it's just a question without phone/time/direct command -> NOT direct booking execution (Let Gemini handle the friendly inquiry reply)
155:         if has_question and not (has_phone or has_explicit_time or has_direct_command):
156:             return False
157: 
158:         # Trigger actual Google Calendar booking execution if explicit details or commands are present
159:         if has_phone or has_explicit_time or has_direct_command:
160:             return True
161: 
162:         return False


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

    async def generate_response(self, user_message: str, history: list = None, db: AsyncSession = None) -> tuple[str, bool, dict]:
        fallback_msg = await self.get_fallback_message(db) if db else DEFAULT_FALLBACK_MESSAGE
        token_stats = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Fast Instant Path for simple Greetings & Check-ins (50ms response time)
        clean_query = user_message.strip().lower().rstrip(".!?")
        greetings = [
            "hey", "hlw", "hello", "hi", "hy", "hei",
            "keo asen", "keu asen", "keo asea", "keo acen", "keu acen",
            "কেউ আছেন", "কেউ আছো", "কেউ কি আছেন", "আছো কেউ", "আছেন কেউ",
            "হাই", "হ্যালো", "আসসালামু আলাইকুম", "assalamu alaikum", "slm", "slam"
        ]
        if clean_query in greetings:
            instant_reply = "জি, আমি আছি! POSTech Live-এ আপনাকে স্বাগত। 😊 আমি আপনাকে কীভাবে সাহায্য করতে পারি বলুন?"
            return instant_reply, True, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

        try:
            if db and await self.is_booking_intent(user_message, db):
                cal_config = await self.get_calendar_config(db)
                booking_msg = await self._handle_calendar_booking(user_message, cal_config, db)
                return booking_msg, False, token_stats

            api_key, model_name = await self.get_gemini_config(db) if db else (settings.GEMINI_API_KEY, "gemini-2.0-flash")
            if not api_key:
                api_key = settings.GEMINI_API_KEY

            if api_key:
                api_key = api_key.strip().strip('"').strip("'")

            if not api_key or api_key.startswith("your_") or not genai:
                print(f"[AI SERVICE WARNING] Missing or invalid Gemini API Key ('{api_key[:10] if api_key else 'None'}')! Returning fallback message.")
                return fallback_msg, False, token_stats

            self._ensure_genai_configured(api_key)

            sys_prompt = await self.get_system_prompt(db) if db else DEFAULT_SYSTEM_PROMPT
            kb_text, _ = await self.get_knowledge_base_data(db) if db else ("Empty", "empty")

            full_prompt = f"{sys_prompt}\n\n## KNOWLEDGE BASE DATA:\n{kb_text}\n\n## CUSTOMER QUERY:\n{user_message}"

            model = genai.GenerativeModel(model_name)
            
            import asyncio
            try:
                response = await asyncio.to_thread(model.generate_content, full_prompt)
                ai_text = response.text.strip() if response and hasattr(response, 'text') else fallback_msg
            except Exception as gem_err:
                print(f"[GEMINI API CALL ERROR] {gem_err}")
                try:
                    response = await model.generate_content_async(full_prompt)
                    ai_text = response.text.strip() if response and hasattr(response, 'text') else fallback_msg
                except Exception as inner_err:
                    print(f"[GEMINI ASYNC FALLBACK ERROR] {inner_err}")
                    return fallback_msg, False, token_stats

            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                token_stats["prompt_tokens"] = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
                token_stats["completion_tokens"] = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
                token_stats["total_tokens"] = getattr(response.usage_metadata, 'total_token_count', 0) or (token_stats["prompt_tokens"] + token_stats["completion_tokens"])

            return ai_text, False, token_stats
        except Exception as e:
            print(f"[AI SERVICE ERROR] {e}")
            return fallback_msg, False, token_stats

ai_service = AIService()
