import httpx
from app.config import settings

class WhatsAppService:
    async def send_text_message(self, recipient_id: str, text: str, access_token: str = None, phone_number_id: str = None) -> bool:
        token = access_token or settings.WA_ACCESS_TOKEN
        phone_id = phone_number_id or settings.WA_PHONE_NUMBER_ID

        if not token or not phone_id or token.startswith("your_"):
            print("WhatsApp API credentials missing!")
            return False

        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "text",
            "text": {"body": text}
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    return True
                else:
                    print(f"WhatsApp API error: {response.json()}")
                    return False
            except Exception as e:
                print(f"Failed to send WhatsApp message: {e}")
                return False

whatsapp_service = WhatsAppService()
