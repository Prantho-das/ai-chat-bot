import re
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Lead
from app.services.log_service import log_service

class LeadExtractorService:
    @staticmethod
    def extract_contact_info(text: str) -> tuple[str | None, str | None]:
        """Extract email and phone from user chat text."""
        email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        email = email_match.group(0) if email_match else None

        phone_match = re.search(r'(?:\+?88)?01[3-9]\d{8}', text)
        phone = phone_match.group(0) if phone_match else None

        return email, phone

    @staticmethod
    def classify_intent(text: str) -> str:
        """Classify user chat intent."""
        lowered = text.lower()
        
        price_keywords = ["দাম", "কতো", "কত", "price", "cost", "pkg", "package", "rate", "ফি", "fee", "কত টাকা", "চার্জ"]
        high_interest_keywords = ["কিনব", "নিব", "নিতে চাই", "অর্ডার", "order", "buy", "purchase", "interested", "আগ্রহী", "কনফার্ম"]
        booking_keywords = ["appointment", "meeting", "মিটিং", "বুকিং", "book", "slot", "শিডিউল", "কখন দেখা"]

        if any(w in lowered for w in high_interest_keywords):
            return "High Interest"
        if any(w in lowered for w in price_keywords):
            return "Price Inquiry"
        if any(w in lowered for w in booking_keywords):
            return "Booking Request"
        
        return "General Inquiry"

    async def process_chat_lead(
        self,
        db: AsyncSession,
        sender_id: str,
        platform: str,
        user_text: str,
        sender_name: str | None = None
    ) -> Lead | None:
        """Process incoming chat message to automatically extract lead details & save to database."""
        extracted_email, extracted_phone = self.extract_contact_info(user_text)
        intent = self.classify_intent(user_text)
        
        is_valuable_lead = bool(extracted_email or extracted_phone or intent in ["High Interest", "Price Inquiry", "Booking Request"])

        if not is_valuable_lead:
            return None

        # Check existing lead
        stmt = select(Lead).where(Lead.sender_id == sender_id, Lead.platform == platform)
        res = await db.execute(stmt)
        lead = res.scalar_one_or_none()

        if lead:
            if extracted_email and not lead.email:
                lead.email = extracted_email
            if extracted_phone and not lead.phone:
                lead.phone = extracted_phone
            if sender_name and not lead.customer_name:
                lead.customer_name = sender_name
            
            lead.intent = intent
            lead.last_message = user_text
            lead.status = "Hot Lead"
            lead.updated_at = datetime.utcnow()
        else:
            lead = Lead(
                sender_id=sender_id,
                platform=platform,
                customer_name=sender_name or f"User {sender_id[-4:]}",
                email=extracted_email,
                phone=extracted_phone,
                intent=intent,
                status="Hot Lead",
                last_message=user_text
            )
            db.add(lead)

        await db.commit()
        await db.refresh(lead)

88:         await log_service.log(
89:             level="SUCCESS",
90:             source="Lead Extractor",
91:             message=f"🔥 Hot Lead captured from {platform.title()} ({sender_id}): Intent '{intent}'",
92:             details=f"Email: {extracted_email or 'N/A'} | Phone: {extracted_phone or 'N/A'}"
93:         )

        return lead

lead_extractor_service = LeadExtractorService()
