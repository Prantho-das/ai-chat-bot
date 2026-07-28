import httpx
from app.config import settings

class MailchimpService:
    async def add_subscriber(self, email: str, name: str = "", tags: list = None, api_key: str = None, list_id: str = None, server_prefix: str = None) -> dict:
        key = api_key or settings.MAILCHIMP_API_KEY
        l_id = list_id or settings.MAILCHIMP_LIST_ID
        prefix = server_prefix or settings.MAILCHIMP_SERVER_PREFIX

        if not key or not l_id:
            return {"success": False, "message": "Mailchimp API Key or List ID missing."}

        if not prefix and "-" in key:
            prefix = key.split("-")[-1]

        if not prefix:
            return {"success": False, "message": "Mailchimp Server Prefix (e.g. us21) missing."}

        url = f"https://{prefix}.api.mailchimp.com/3.0/lists/{l_id}/members"
        
        first_name = name.split()[0] if name else ""
        last_name = " ".join(name.split()[1:]) if name and len(name.split()) > 1 else ""

        payload = {
            "email_address": email,
            "status": "subscribed",
            "merge_fields": {
                "FNAME": first_name,
                "LNAME": last_name
            }
        }
        if tags:
            payload["tags"] = tags

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, auth=("anystring", key))
                res_data = response.json()
                if response.status_code in [200, 201]:
                    return {"success": True, "data": res_data}
                elif res_data.get("title") == "Member Exists":
                    return {"success": True, "message": "Member already subscribed."}
                else:
                    return {"success": False, "message": res_data.get("detail", "Mailchimp API Error")}
            except Exception as e:
                return {"success": False, "message": str(e)}

mailchimp_service = MailchimpService()
