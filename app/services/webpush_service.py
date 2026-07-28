import json
import httpx
from app.config import settings

class WebPushService:
    async def send_notification(self, subscription_info: dict, title: str, body: str, private_key: str = None, claims_email: str = None) -> dict:
        priv_key = private_key or settings.VAPID_PRIVATE_KEY
        email = claims_email or settings.VAPID_CLAIMS_EMAIL

        if not priv_key:
            return {"success": False, "message": "VAPID Private Key missing."}

        endpoint = subscription_info.get("endpoint")
        if not endpoint:
            return {"success": False, "message": "Subscription endpoint missing."}

        payload = json.dumps({
            "title": title,
            "body": body,
            "icon": "/static/icon.png",
            "url": "/"
        })

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    endpoint,
                    content=payload,
                    headers={"Content-Type": "application/json", "TTL": "60"}
                )
                if res.status_code in [200, 201, 202]:
                    return {"success": True}
                return {"success": False, "message": f"WebPush endpoint returned {res.status_code}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

webpush_service = WebPushService()
