import httpx
import json
from app.config import settings
from app.services.log_service import log_service

class MessengerService:
    def __init__(self):
        self.api_url = "https://graph.facebook.com/v19.0/me/messages"

    async def send_text_message(self, recipient_id: str, text: str, access_token: str = None) -> bool:
        token = access_token or getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
        if not token or token.startswith("your_"):
            await log_service.log("ERROR", "Messenger", f"Cannot send DM to {recipient_id}: Page Access Token missing or invalid.", "Set FB Page Access Token in Bot Settings.")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text}
        }
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                res_data = response.json()
                if response.status_code == 200:
                    await log_service.log("SUCCESS", "Messenger", f"Successfully sent DM reply to user {recipient_id}", json.dumps(res_data))
                    return True
                else:
                    err_msg = res_data.get("error", {}).get("message", json.dumps(res_data))
                    await log_service.log("ERROR", "Messenger", f"FB DM Send Failed ({response.status_code}): {err_msg}", json.dumps(res_data))
                    return False
            except Exception as e:
                await log_service.log("ERROR", "Messenger", f"Network Exception while sending FB DM: {e}")
                return False

    async def send_image_message(self, recipient_id: str, image_url: str, access_token: str = None) -> bool:
        """Send an image to a Messenger user via URL."""
        token = access_token or getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
        if not token or token.startswith("your_"):
            await log_service.log("ERROR", "Messenger", f"Cannot send image to {recipient_id}: Token missing.")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": image_url, "is_reusable": True}
                }
            }
        }
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                res_data = response.json()
                if response.status_code == 200:
                    await log_service.log("SUCCESS", "Messenger", f"Image sent to {recipient_id}", json.dumps(res_data))
                    return True
                else:
                    err_msg = res_data.get("error", {}).get("message", json.dumps(res_data))
                    await log_service.log("ERROR", "Messenger", f"Image send failed ({response.status_code}): {err_msg}", json.dumps(res_data))
                    return False
            except Exception as e:
                await log_service.log("ERROR", "Messenger", f"Network exception sending image: {e}")
                return False

    async def send_attachment_message(self, recipient_id: str, attachment_type: str, url: str, access_token: str = None) -> bool:
        """Send any attachment (image/audio/video/file) to a Messenger user."""
        token = access_token or getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
        if not token or token.startswith("your_"):
            await log_service.log("ERROR", "Messenger", f"Cannot send {attachment_type} to {recipient_id}: Token missing.")
            return False

        if attachment_type not in ("image", "audio", "video", "file"):
            await log_service.log("ERROR", "Messenger", f"Invalid attachment type: {attachment_type}")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": attachment_type,
                    "payload": {"url": url, "is_reusable": True}
                }
            }
        }
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                res_data = response.json()
                if response.status_code == 200:
                    await log_service.log("SUCCESS", "Messenger", f"{attachment_type} sent to {recipient_id}", json.dumps(res_data))
                    return True
                else:
                    err_msg = res_data.get("error", {}).get("message", json.dumps(res_data))
                    await log_service.log("ERROR", "Messenger", f"{attachment_type} send failed ({response.status_code}): {err_msg}", json.dumps(res_data))
                    return False
            except Exception as e:
                await log_service.log("ERROR", "Messenger", f"Network exception sending {attachment_type}: {e}")
                return False

    async def send_generic_template(self, recipient_id: str, elements: list[dict], access_token: str = None) -> bool:
        """Send a generic template (carousel cards with image, title, subtitle, buttons)."""
        token = access_token or getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
        if not token or token.startswith("your_"):
            await log_service.log("ERROR", "Messenger", f"Cannot send template to {recipient_id}: Token missing.")
            return False

        payload = {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": elements
                    }
                }
            }
        }
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.api_url, json=payload, params=params)
                res_data = response.json()
                if response.status_code == 200:
                    await log_service.log("SUCCESS", "Messenger", f"Template sent to {recipient_id}", json.dumps(res_data))
                    return True
                else:
                    err_msg = res_data.get("error", {}).get("message", json.dumps(res_data))
                    await log_service.log("ERROR", "Messenger", f"Template send failed ({response.status_code}): {err_msg}", json.dumps(res_data))
                    return False
            except Exception as e:
                await log_service.log("ERROR", "Messenger", f"Network exception sending template: {e}")
                return False

    async def reply_to_comment(self, comment_id: str, text: str, access_token: str = None) -> bool:
        token = access_token or getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
        if not token or token.startswith("your_"):
            await log_service.log("ERROR", "FB Comment", f"Cannot reply to comment {comment_id}: Page Access Token missing.", "Set FB Page Access Token in Bot Settings.")
            return False

        url = f"https://graph.facebook.com/v19.0/{comment_id}/comments"
        payload = {"message": text}
        params = {"access_token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, params=params)
                res_data = response.json()
                if response.status_code in [200, 201]:
                    await log_service.log("SUCCESS", "FB Comment", f"Successfully replied to comment {comment_id}", json.dumps(res_data))
                    return True
                else:
                    err_msg = res_data.get("error", {}).get("message", json.dumps(res_data))
                    await log_service.log("ERROR", "FB Comment", f"FB Comment Reply Failed ({response.status_code}): {err_msg}", json.dumps(res_data))
                    return False
            except Exception as e:
                await log_service.log("ERROR", "FB Comment", f"Network Exception while replying to comment: {e}")
                return False

messenger_service = MessengerService()
