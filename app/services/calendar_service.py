from datetime import datetime, timedelta
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.config import settings

class CalendarService:
    def __init__(self):
        pass

    def _get_service(self, token_json_str: str):
        try:
            token_data = json.loads(token_json_str)
            creds = Credentials.from_authorized_user_info(token_data, scopes=['https://www.googleapis.com/auth/calendar'])
            return build('calendar', 'v3', credentials=creds)
        except Exception as e:
            print(f"Error initializing Google Calendar client: {e}")
            return None

    async def create_event(self, token_json_str: str, summary: str, description: str, start_time_iso: str, duration_minutes: int = 30) -> dict:
        service = self._get_service(token_json_str)
        if not service:
            return {"success": False, "message": "Google Calendar connection failed."}

        try:
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=duration_minutes)

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

            event = service.events().insert(calendarId='primary', body=event_body).execute()
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
