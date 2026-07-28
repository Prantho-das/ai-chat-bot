from datetime import datetime, timedelta
import json
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from app.config import settings

SCOPES = ['https://www.googleapis.com/auth/calendar']

class CalendarService:
    def _get_service(self, calendar_config: dict):
        try:
            creds = None
            token_json_str = calendar_config.get("google_calendar_token", "")
            client_id = calendar_config.get("google_client_id", "")
            client_secret = calendar_config.get("google_client_secret", "")
            refresh_token = calendar_config.get("google_refresh_token", "")

            if token_json_str and token_json_str.strip().startswith("{"):
                token_data = json.loads(token_json_str)
                if token_data.get("type") == "service_account":
                    creds = service_account.Credentials.from_service_account_info(token_data, scopes=SCOPES)
                else:
                    creds = Credentials.from_authorized_user_info(token_data, scopes=SCOPES)
            elif refresh_token and client_id and client_secret:
                creds = Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=client_id,
                    client_secret=client_secret,
                    scopes=SCOPES
                )

            if creds:
                return build('calendar', 'v3', credentials=creds)
        except Exception as e:
            print(f"Error initializing Google Calendar client: {e}")
        return None

    async def create_event(
        self,
        calendar_config: dict,
        summary: str,
        description: str,
        start_time_iso: str,
        duration_minutes: int = 30,
        attendee_email: str = None
    ) -> dict:
        service = self._get_service(calendar_config)
        if not service:
            return {"success": False, "message": "Google Calendar connection failed or missing credentials."}

        try:
            # Parse start time ISO
            if start_time_iso.endswith('Z'):
                start_time_iso = start_time_iso[:-1]
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=duration_minutes)

            calendar_id = calendar_config.get("google_calendar_id") or "primary"

            event_body = {
                'summary': summary,
                'description': description,
                'start': {
                    'dateTime': start_dt.isoformat(),
                    'timeZone': 'Asia/Dhaka',
                },
                'end': {
                    'dateTime': end_dt.isoformat(),
                    'timeZone': 'Asia/Dhaka',
                },
            }

            if attendee_email:
                event_body['attendees'] = [{'email': attendee_email}]

            event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            return {
                "success": True,
                "event_id": event.get('id'),
                "html_link": event.get('htmlLink'),
                "start_time": start_dt.strftime('%Y-%m-%d %H:%M')
            }
        except Exception as e:
            print(f"Error creating calendar event: {e}")
            return {"success": False, "message": str(e)}

calendar_service = CalendarService()
