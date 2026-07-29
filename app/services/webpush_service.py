import json
import asyncio
from pywebpush import webpush, WebPushException
from app.config import settings

class WebPushService:
    def _send_sync(self, subscription_info: dict, payload: str, private_key: str, claims_email: str):
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": f"mailto:{claims_email}"}
        )

    async def send_notification(self, subscription_info: dict, title: str, body: str, private_key: str = None, claims_email: str = None) -> dict:
        endpoint = subscription_info.get("endpoint")
        if not endpoint:
            return {"success": False, "message": "Subscription endpoint missing."}

        priv_key = private_key or getattr(settings, "VAPID_PRIVATE_KEY", "")
        email = claims_email or getattr(settings, "VAPID_CLAIMS_EMAIL", "admin@example.com")

        if not priv_key:
            return {"success": False, "message": "VAPID Private Key is missing."}

        payload = json.dumps({
            "title": title,
            "body": body,
            "icon": "/static/icon.png",
            "url": "/"
        })

        try:
            loop = asyncio.get_running_loop()
            # Send WebPush using thread executor since pywebpush is synchronous/blocking
            await loop.run_in_executor(
                None,
                self._send_sync,
                subscription_info,
                payload,
                priv_key,
                email
            )
            return {"success": True}
        except WebPushException as ex:
            # If subscriber is gone or expired, it's captured here
            return {"success": False, "message": f"WebPushException: {ex}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

webpush_service = WebPushService()
