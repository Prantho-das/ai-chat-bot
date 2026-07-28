import httpx
from app.config import settings

class FCMService:
    async def send_push_notification(self, title: str, body: str, target_token: str = None, server_key: str = None) -> dict:
        key = server_key or settings.FCM_SERVER_KEY
        if not key:
            return {"success": False, "message": "FCM Server Key missing."}

        url = "https://fcm.googleapis.com/fcm/send"
        headers = {
            "Authorization": f"key={key}",
            "Content-Type": "application/json"
        }

        payload = {
            "to": target_token or "/topics/all",
            "notification": {
                "title": title,
                "body": body,
                "sound": "default"
            },
            "data": {
                "click_action": "FLUTTER_NOTIFICATION_CLICK"
            }
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    return {"success": True, "result": response.json()}
                else:
                    return {"success": False, "message": f"FCM Error: {response.text}"}
            except Exception as e:
                return {"success": False, "message": str(e)}

fcm_service = FCMService()
