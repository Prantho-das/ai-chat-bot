import hashlib
import json
import math
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
from app.services.log_service import log_service
from app.helpers import get_bot_setting, convert_bn_to_en

DEFAULT_FALLBACK_MESSAGE = (
    "দুঃখিত, এই মুহূর্তে উত্তর তৈরিতে সামান্য সমস্যা হচ্ছে। "
    "খুব দ্রুত আমাদের একজন প্রতিনিধি আপনার সাথে যোগাযোগ করবেন."
)

DEFAULT_SYSTEM_PROMPT = (
    "তুমি একজন অত্যন্ত বুদ্ধিমান, আন্তরিক এবং প্রফেশনাল AI কাস্টমার সাপোর্ট স্পেশালিস্ট। "
    "কথোপকথন সবসময় ব্যালেন্সড, আকর্ষণীয় এবং রিডেবল রাখবে:\n\n"
    "## স্মার্ট রেসপন্স রুলস:\n"
    "১. **পরিমিত ও আকর্ষণীয় উত্তর (Max 3-4 Lines):** বিস্তারিত বিষয় হলেও চ্যাটে কখনো বিশাল লম্বা এসে (Essay) বা অতিরিক্ত পয়েন্ট দেবে না। মূল ৩-৪টি গুরুত্বপূর্ণ পয়েন্ট সংক্ষেপে ও স্পষ্ট করে তুলে ধরো।\n"
    "২. **সহজ প্রশ্ন (Greetings, ঠিকানা, মূল্য):** ১-২ লাইনে সরাসরি উত্তর দাও।\n"
    "৩. **তথ্য উৎস:** শুধুমাত্র প্রদত্ত Knowledge Base থেকে সঠিক তথ্য প্রকাশ করবে।\n"
    "৪. **মেসেঞ্জার ফ্রেন্ডলি:** পড়া সহজ হয় এমনভাবে সুন্দর ফর্মেটিংয়ে লিখবে।"
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

    def _extract_api_keys(self, raw_keys: str) -> list[str]:
        if not raw_keys:
            return []
        keys = [k.strip().strip('"').strip("'") for k in raw_keys.split(",") if k.strip()]
        return [k for k in keys if k and not k.startswith("your_")]

    def _generate_fallback_vector(self, text: str) -> str:
        if not text:
            return None
        vec = [0.0] * 64
        words = re.findall(r'\w+', text.lower())
        for w in words:
            h = int(hashlib.md5(w.encode('utf-8')).hexdigest(), 16)
            idx = h % 64
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [round(v / norm, 5) for v in vec]
        return json.dumps(vec)

    async def generate_embedding(self, text: str, db: AsyncSession = None) -> str:
        try:
            if not text:
                return None
            raw_key, _ = await self.get_gemini_config(db) if db else (settings.GEMINI_API_KEY, "gemini-2.0-flash")
            keys = self._extract_api_keys(raw_key) or self._extract_api_keys(settings.GEMINI_API_KEY)
            
            if genai and keys:
                for key in keys:
                    try:
                        self._ensure_genai_configured(key)
                        for model_name in ["models/text-embedding-004", "text-embedding-004", "models/embedding-001"]:
                            try:
                                result = genai.embed_content(model=model_name, content=text[:2000], task_type="retrieval_document")
                                if result and "embedding" in result:
                                    return json.dumps(result["embedding"])
                            except Exception as embed_err:
                                print(f"[EMBEDDING MODEL TRY FAIL] {model_name}: {embed_err}")
                                continue
                    except Exception as key_err:
                        print(f"[EMBEDDING KEY FAIL] Key ending in ...{key[-6:]}: {key_err}")
                        continue
        except Exception as e:
            print(f"[EMBEDDING ERROR] {e}")

        # Local fallback vector generation so embedding never stays empty or fails
        return self._generate_fallback_vector(text)


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

    async def get_knowledge_base_data_with_entries(self, db: AsyncSession, user_message: str = "") -> tuple[str, str, list, str]:
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True).order_by(KnowledgeEntry.category, KnowledgeEntry.id)
        result = await db.execute(stmt)
        entries = result.scalars().all()

        if not entries:
            return "কোনো অতিরিক্ত তথ্য প্রদান করা হয়নি।", "empty_kb", [], "none", None

        selected_entries = []
        search_method = "none"

        if user_message:
            cleaned_msg = user_message.lower().strip()
            query_words = set(re.findall(r'\w+', cleaned_msg))

            # 1. Hybrid Search (Vector Similarity + Keyword Boosting)
            query_embedding_json = await self.generate_embedding(user_message, db=db)
            hybrid_scores = []

            query_vec = None
            if query_embedding_json:
                try:
                    query_vec = json.loads(query_embedding_json)
                except Exception:
                    query_vec = None

            for entry in entries:
                v_score = 0.0
                if query_vec and entry.embedding_json:
                    try:
                        doc_vec = json.loads(entry.embedding_json)
                        dot = sum(q * d for q, d in zip(query_vec, doc_vec))
                        norm_q = math.sqrt(sum(q * q for q in query_vec))
                        norm_d = math.sqrt(sum(d * d for d in doc_vec))
                        v_score = dot / (norm_q * norm_d) if (norm_q * norm_d) > 0 else 0.0
                    except Exception:
                        v_score = 0.0

                # Keyword boost calculation
                title_words = set(re.findall(r'\w+', (entry.title or "").lower()))
                content_words = set(re.findall(r'\w+', (entry.content or "").lower()))
                k_score = (len(query_words.intersection(title_words)) * 0.15) + (len(query_words.intersection(content_words)) * 0.05)
                if (entry.category or "") and entry.category.lower() in cleaned_msg:
                    k_score += 0.1

                final_score = (v_score * 0.7) + (k_score * 0.3) if query_vec else k_score
                min_threshold = 0.25 if query_vec else 0.05
                if final_score > min_threshold:
                    hybrid_scores.append((final_score, entry))

            if hybrid_scores:
                hybrid_scores.sort(key=lambda x: x[0], reverse=True)
                selected_entries = [item[1] for item in hybrid_scores[:2]]
                search_method = "hybrid_vector" if query_vec else "keyword_fallback"
            elif not query_vec:
                selected_entries = entries[:2]
                search_method = "default_fallback"
        else:
            selected_entries = entries[:3]
            search_method = "default"

        if not selected_entries:
            return "কোনো প্রাসঙ্গিক নলেজ ডকু দেওয়া হয়নি।", "no_rag_match", [], search_method, query_embedding_json

        kb_lines = []
        for idx, entry in enumerate(selected_entries, 1):
            cat = (entry.category or 'GENERAL').upper()
            kb_lines.append(f"{idx}. [{cat}] {entry.title or ''}: {entry.content or ''}")

        kb_text = "\n".join(kb_lines)
        kb_hash = hashlib.md5(kb_text.encode("utf-8")).hexdigest()
        return kb_text, kb_hash, selected_entries, search_method, query_embedding_json


    async def get_gemini_config(self, db: AsyncSession) -> tuple[str, str]:
        stmt = select(BotSetting).where(BotSetting.key.in_(["gemini_api_key", "gemini_model"]))
        result = await db.execute(stmt)
        records = result.scalars().all()
        settings_dict = {r.key: r.value for r in records}

        api_key = settings_dict.get("gemini_api_key", "")
        model_name = settings_dict.get("gemini_model", "").strip()
        
        # Fallback to stable valid models if empty or invalid model selected
        if not model_name or "tts" in model_name.lower():
            model_name = "gemini-2.0-flash"

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
                    # Filter out TTS/Audio only models that fail standard text chat
                    valid_models = [m for m in models if "tts" not in m["id"].lower() and "audio" not in m["id"].lower()]
                    return valid_models if valid_models else models
        except Exception as e:
            print(f"Error fetching models from Gemini API: {e}")

        return [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash (Recommended)"},
            {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
            {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro"}
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

        # Get configured booking keywords
        booking_kws = await self.get_booking_keywords(db)
        # Check if message contains any booking keyword
        has_booking_kw = any(kw in lowered for kw in booking_kws)

        if not has_booking_kw:
            return False

        question_indicators = ["kora jabe", "hobe ki", "jabe ki", "jabe?", "hobe?", "kivabe", "pari ki", "করা যাবে", "হবে কি", "যাবে কি", "কীভাবে", "পারি কি"]
        has_question = any(q in lowered for q in question_indicators)

        has_phone = bool(re.search(r'01[3-9]\d{8}', lowered))
        has_explicit_time = bool(re.search(r'\b(\d{1,2}|[০-৯]{1,2})\s*(?:ta|tai|tar|টার|টা|pm|am|:\d\d)\b', lowered))

        direct_commands = ["book koro", "fix koro", "book korin", "booking den", "book den", "বুক করুন", "বুক করে দেন", "মিটিং ফিক্স করুন"]
        has_direct_command = any(cmd in lowered for cmd in direct_commands)

        if has_question and not (has_phone or has_explicit_time or has_direct_command):
            return False

        if has_phone or has_explicit_time or has_direct_command:
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

        link_str = f" Link: {cal_res.get('html_link', '')}" if cal_res.get('html_link') else ""
        if is_rescheduled:
            return f"[SYSTEM ACTION: Requested slot on {final_date} at {formatted_time} was busy. Google Calendar booked next available slot for {final_date} at {final_time}. Clearly confirm this date ({final_date}) and time ({final_time}) to customer.{link_str}]"
        return f"[SYSTEM ACTION: Google Calendar appointment successfully booked for {final_date} at {final_time}. Clearly confirm this exact date ({final_date}) and time ({final_time}) and provide calendar link to customer.{link_str}]"

    async def generate_response(
        self,
        user_message: str,
        history: list = None,
        db: AsyncSession = None,
        image_bytes: bytes = None,
        image_mime: str = None,
        audio_bytes: bytes = None,
        audio_mime: str = None
    ) -> tuple[str, bool, dict]:
        fallback_msg = await self.get_fallback_message(db) if db else DEFAULT_FALLBACK_MESSAGE
        token_stats = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        try:
            user_message = user_message or ""
            clean_raw = user_message.strip().lower()

            # 1. Static Greeting Bypass (0 Tokens, Instant Reply) - Only if no media attached
            if not image_bytes and not audio_bytes:
                simple_greetings = {"hi", "hello", "hey", "হাই", "হ্যালো", "হে", "হেই", "assalamu alaikum", "assalamualaikum", "সালামু আলাইকুম", "আসসালামু আলাইকুম", "কেমন আছেন", "kemon achen", "kemon asen"}
                if clean_raw in simple_greetings:
                    greeting_reply = "হ্যালো! আপনাকে কীভাবে সাহায্য করতে পারি? আমাদের সফটওয়্যার বা সার্ভিস সম্পর্কে বিস্তারিত জানতে কোনো প্রশ্ন থাকলে বলতে পারেন।"
                    return greeting_reply, True, {
                        "matched_count": 0,
                        "matched_titles": [],
                        "search_method": "static_rule",
                        "model_used": "Static Rule (0 Tokens)",
                        "tokens": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    }

            booking_action_info = ""
            if db and await self.is_booking_intent(user_message, db):
                cal_config = await self.get_calendar_config(db)
                booking_action_info = await self._handle_calendar_booking(user_message, cal_config, db)

            raw_key, model_name = await self.get_gemini_config(db) if db else (settings.GEMINI_API_KEY, "gemini-2.0-flash")
            keys = self._extract_api_keys(raw_key) or self._extract_api_keys(settings.GEMINI_API_KEY)

            if not keys or not genai:
                print("[AI SERVICE WARNING] Missing or invalid Gemini API Key! Returning fallback message.")
                return fallback_msg, False, token_stats

            sys_prompt = await self.get_system_prompt(db) if db else DEFAULT_SYSTEM_PROMPT
            
            # Enforce dynamic response length instruction
            resp_len = await self.get_response_length(db) if db else "short"
            
            # Check if user query contains detail keywords (e.g. price, features, details) to auto-upgrade to medium/long response
            detail_kws = await self.get_detail_keywords(db) if db else []
            has_detail_kw = any(kw in user_message.lower() for kw in detail_kws) if detail_kws else False
            
            if resp_len == "short" and has_detail_kw:
                resp_len = "medium"  # Auto-upgrade to medium to allow displaying list/prices

            if resp_len == "short":
                sys_prompt += "\n\n## RESPONSE LENGTH CRITICAL RULE:\nউত্তর অবশ্যই অত্যন্ত সংক্ষিপ্ত এবং সর্বোচ্চ ১ থেকে ২ লাইনের মধ্যে হতে হবে। কোনো অতিরিক্ত বিবরণ বা পয়েন্ট আকারে বড় তালিকা দেওয়া যাবে না।"
            elif resp_len == "medium":
                sys_prompt += "\n\n## RESPONSE LENGTH RULE:\nউত্তরটি মাঝারি মানের হবে, ৩ থেকে ৪ লাইনের মধ্যে শেষ করবে।"
            elif resp_len == "long":
                sys_prompt += "\n\n## RESPONSE LENGTH RULE:\nবিস্তারিত উত্তর প্রদান করো।"

            # Add multimodal instruction if media attached
            if image_bytes:
                sys_prompt += "\n\n[NOTE: কাস্টমার একটি ছবি পাঠিয়েছেন। ছবি এবং কাস্টমারের মেসেজটি বিশ্লেষণ করে প্রাসঙ্গিক উত্তর দাও।]"
            if audio_bytes:
                sys_prompt += "\n\n[NOTE: কাস্টমার একটি ভয়েস/অডিও মেসেজ পাঠিয়েছেন। অডিওটি শুনে বিশ্লেষণ করে বাংলায় সরাসরি উত্তর দাও।]"

            # 2. Normalized Query for Higher Cache Hits
            clean_q = re.sub(r'[^\w\s]', '', clean_raw).strip()
            kb_text, kb_hash, selected_entries, search_method, query_emb_json = await self.get_knowledge_base_data_with_entries(db, user_message) if db else ("Empty", "empty", [], "none", None)

            cache_key = hashlib.md5(f"{clean_q}:{kb_hash}".encode("utf-8")).hexdigest()
            if db and not booking_action_info and not image_bytes and not audio_bytes:
                # 1. Exact MD5 Hash Match
                stmt_c = select(CacheEntry).where(CacheEntry.prompt_hash == cache_key)
                res_c = await db.execute(stmt_c)
                cached = res_c.scalar_one_or_none()

                # 2. Semantic Keyword Cache Match Fallback
                if not cached and selected_entries:
                    query_words = set(re.findall(r'\w+', clean_q.lower()))
                    if query_words:
                        stmt_all_c = select(CacheEntry).order_by(CacheEntry.created_at.desc()).limit(100)
                        res_all_c = await db.execute(stmt_all_c)
                        all_cached = res_all_c.scalars().all()
                        for c_item in all_cached:
                            c_words = set(re.findall(r'\w+', (c_item.user_query or "").lower()))
                            intersection = query_words.intersection(c_words)
                            union = query_words.union(c_words)
                            jaccard = len(intersection) / len(union) if union else 0.0
                            if jaccard >= 0.60:
                                cached = c_item
                                break

                if cached and cached.ai_response:
                    return cached.ai_response, True, {
                        "matched_count": len(selected_entries),
                        "matched_titles": [f"[{e.category.upper()}] {e.title}" for e in selected_entries[:4]],
                        "search_method": search_method + " (Smart Cache)",
                        "model_used": model_name + " (Cached)",
                        "tokens": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    }

            hist_limit_val = await get_bot_setting(db, "max_history_turns") if db else "4"
            try:
                hist_limit = int(hist_limit_val)
            except ValueError:
                hist_limit = 4

            hist_txt = ""
            if history:
                h_lines = []
                for msg in history[-hist_limit:]:
                    if isinstance(msg, dict):
                        role = msg.get("role")
                        content = msg.get("content")
                    else:
                        role = getattr(msg, "role", "")
                        content = getattr(msg, "content", "")
                    r = "Customer" if role == "user" else "Assistant"
                    h_lines.append(f"{r}: {content}")
                if h_lines:
                    hist_txt = "## CONVERSATION HISTORY:\n" + "\n".join(h_lines) + "\n\n"

            full_prompt = f"{sys_prompt}\n\n## KNOWLEDGE BASE DATA:\n{kb_text}\n\n"
            if hist_txt:
                full_prompt += hist_txt
            if booking_action_info:
                full_prompt += f"## SYSTEM ACTION COMPLETED:\n{booking_action_info}\n\n"
            full_prompt += f"## CUSTOMER QUERY:\n{user_message if user_message else 'Customer sent media file.'}"

            models_to_try = [model_name]
            if model_name != "gemini-2.0-flash":
                models_to_try.append("gemini-2.0-flash")

            response = None
            ai_text = None

            for key in keys:
                self._ensure_genai_configured(key)
                for m_name in models_to_try:
                    try:
                        model = genai.GenerativeModel(m_name)
                        
                        prompt_contents = []
                        if image_bytes and image_mime:
                            prompt_contents.append({"mime_type": image_mime, "data": image_bytes})
                        if audio_bytes and audio_mime:
                            prompt_contents.append({"mime_type": audio_mime, "data": audio_bytes})
                        prompt_contents.append(full_prompt)

                        res = await model.generate_content_async(prompt_contents)

                        if res:
                            t = None
                            try:
                                t = res.text.strip() if hasattr(res, 'text') else None
                            except ValueError:
                                if hasattr(res, 'candidates') and res.candidates:
                                    parts = res.candidates[0].content.parts
                                    t = "".join([p.text for p in parts if hasattr(p, 'text') and p.text]).strip()
                            if t:
                                ai_text = t
                                response = res
                                model_name = m_name
                                break
                    except Exception as gem_err:
                        err_text = f"Gemini API Exception for key ending in ...{key[-6:]} / {m_name} ({type(gem_err).__name__}): {gem_err}"
                        print(f"[GEMINI API ERROR] {err_text}")
                        await log_service.log("ERROR", "AI Engine Error", err_text, f"Model: {m_name}")
                        continue
                if ai_text:
                    break
                if ai_text:
                    break

            if not ai_text:
                return fallback_msg, False, {
                    "matched_count": len(selected_entries),
                    "matched_titles": [f"[{e.category.upper()}] {e.title}" for e in selected_entries[:4]],
                    "search_method": search_method,
                    "model_used": model_name,
                    "tokens": token_stats
                }

            if db and ai_text != fallback_msg and not image_bytes and not audio_bytes:
                try:
                    stmt_c = select(CacheEntry).where(CacheEntry.prompt_hash == cache_key)
                    res_c = await db.execute(stmt_c)
                    existing_cache = res_c.scalar_one_or_none()

                    if existing_cache:
                        existing_cache.ai_response = ai_text
                        existing_cache.user_query = user_message[:500]
                    else:
                        new_cache = CacheEntry(
                            prompt_hash=cache_key,
                            user_query=user_message[:500],
                            ai_response=ai_text
                        )
                        db.add(new_cache)
                    await db.commit()
                except Exception as save_err:
                    print(f"[CACHE SAVE ERROR] {save_err}")

            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                token_stats["prompt_tokens"] = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
                token_stats["completion_tokens"] = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
                token_stats["total_tokens"] = getattr(response.usage_metadata, 'total_token_count', 0) or (token_stats["prompt_tokens"] + token_stats["completion_tokens"])

            rag_info = {
                "matched_count": len(selected_entries),
                "matched_titles": [f"[{e.category.upper()}] {e.title}" for e in selected_entries[:4]],
                "search_method": search_method,
                "model_used": model_name,
                "tokens": token_stats
            }
            return ai_text, False, rag_info
        except Exception as e:
            err_msg = f"AI Response Critical Exception: {str(e)}"
            print(f"[AI SERVICE ERROR] {err_msg}")
            if db:
                try:
                    await log_service.log("ERROR", "AI Critical Exception", err_msg)
                except Exception:
                    pass
            return fallback_msg, False, {
                "matched_count": 0,
                "matched_titles": [],
                "search_method": "error",
                "model_used": "unknown",
                "tokens": token_stats
            }

ai_service = AIService()

ai_service = AIService()
