import httpx
from app.config import settings

class WhatsAppService:
    def __init__(self):
        pass

    async def send_text_message(self, recipient_id: str, text: str) -> bool:
        if not settings.WA_ACCESS_TOKEN or not settings.WA_PHONE_NUMBER_ID:
            print("WhatsApp API credentials missing!")
            return False

        url = f"https://graph.facebook.com/v19.0/{settings.WA_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
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
