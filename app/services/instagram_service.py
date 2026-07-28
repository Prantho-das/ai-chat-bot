import httpx
from app.config import settings

class InstagramService:
    def __init__(self):
        self.api_url = "https://graph.facebook.com/v19.0/me/messages"

    async def send_dm(self, recipient_id: str, text: str, access_token: str = None) -> bool:
        token = access_token or settings.IG_ACCESS_TOKEN or settings.FB_PAGE_ACCESS_TOKEN
        if not token or token.startswith("your_"):
            print("Instagram Access Token missing!")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text}
        }
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                if response.status_code == 200:
                    return True
                else:
                    print(f"Instagram DM Error: {response.json()}")
                    return False
            except Exception as e:
                print(f"Failed to send Instagram DM: {e}")
                return False

instagram_service = InstagramService()
