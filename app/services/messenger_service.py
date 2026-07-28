import httpx
from app.config import settings

class MessengerService:
    def __init__(self):
        self.api_url = "https://graph.facebook.com/v19.0/me/messages"

    async def send_text_message(self, recipient_id: str, text: str) -> bool:
        if not settings.FB_PAGE_ACCESS_TOKEN:
            print("FB_PAGE_ACCESS_TOKEN missing!")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text}
        }
        params = {"access_token": settings.FB_PAGE_ACCESS_TOKEN}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                res_data = response.json()
                if response.status_code == 200:
                    return True
                else:
                    print(f"FB API error: {res_data}")
                    return False
            except Exception as e:
                print(f"Failed to send FB message: {e}")
                return False

    async def reply_to_comment(self, comment_id: str, text: str) -> bool:
        if not settings.FB_PAGE_ACCESS_TOKEN:
            print("FB_PAGE_ACCESS_TOKEN missing!")
            return False

        url = f"https://graph.facebook.com/v19.0/{comment_id}/comments"
        payload = {"message": text}
        params = {"access_token": settings.FB_PAGE_ACCESS_TOKEN}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, params=params)
                if response.status_code == 200:
                    return True
                else:
                    print(f"FB Comment Reply Error: {response.json()}")
                    return False
            except Exception as e:
                print(f"Failed to reply to FB comment: {e}")
                return False

messenger_service = MessengerService()

