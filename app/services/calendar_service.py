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

            # Prioritize Service Account JSON Key
            if token_json_str and token_json_str.strip().startswith("{"):
                try:
                    token_data = json.loads(token_json_str)
                    if token_data.get("type") == "service_account":
                        creds = service_account.Credentials.from_service_account_info(token_data, scopes=SCOPES)
                    else:
                        creds = Credentials.from_authorized_user_info(token_data, scopes=SCOPES)
                except Exception as json_e:
                    print(f"Error parsing Service Account JSON token: {json_e}")

            # Fallback to OAuth Refresh Token if valid (and not dummy placeholder)
            if not creds and refresh_token and client_id and client_secret:
                if not client_id.startswith("your_"):
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

    def is_slot_available(self, service, calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
        try:
            target_id = calendar_id if ("@" in calendar_id and not calendar_id.endswith(".gserviceaccount.com")) else "primary"
            events_result = service.events().list(
                calendarId=target_id,
                timeMin=start_dt.strftime('%Y-%m-%dT%H:%M:%S+06:00'),
                timeMax=end_dt.strftime('%Y-%m-%dT%H:%M:%S+06:00'),
                singleEvents=True
            ).execute()
            items = events_result.get('items', [])
            return len(items) == 0
        except Exception as e:
            print(f"Error checking slot availability: {e}")
            return True

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
            if start_time_iso.endswith('Z'):
                start_time_iso = start_time_iso[:-1]
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            
            configured_id = calendar_config.get("google_calendar_id", "").strip()
            user_email = configured_id if ("@" in configured_id and not configured_id.endswith(".gserviceaccount.com")) else None

            target_calendar_id = user_email if user_email else "primary"

            # Check slot availability
            slot_free = self.is_slot_available(service, target_calendar_id, start_dt, end_dt)
            is_rescheduled = False

            if not slot_free:
                is_rescheduled = True
                while not slot_free and start_dt.hour < 20:
                    start_dt = start_dt + timedelta(hours=1)
                    end_dt = start_dt + timedelta(minutes=duration_minutes)
                    slot_free = self.is_slot_available(service, target_calendar_id, start_dt, end_dt)

            event_body = {
                'summary': summary,
                'description': description,
                'start': {
                    'dateTime': start_dt.strftime('%Y-%m-%dT%H:%M:%S+06:00'),
                    'timeZone': 'Asia/Dhaka',
                },
                'end': {
                    'dateTime': end_dt.strftime('%Y-%m-%dT%H:%M:%S+06:00'),
                    'timeZone': 'Asia/Dhaka',
                },
            }

            attendees = []
            if attendee_email:
                attendees.append({'email': attendee_email})
            if user_email and user_email != attendee_email:
                attendees.append({'email': user_email})

            if attendees:
                event_body['attendees'] = attendees

            # 1. Try target_calendar_id
            # 2. Fallback to primary Service Account calendar
            event = None
            try:
                event = service.events().insert(
                    calendarId=target_calendar_id,
                    body=event_body
                ).execute()
            except Exception as inner_e:
                print(f"Direct calendar insert into {target_calendar_id} failed ({inner_e}), inserting into primary calendar...")
                event = service.events().insert(
                    calendarId="primary",
                    body=event_body
                ).execute()

            return {
                "success": True,
                "event_id": event.get('id'),
                "html_link": event.get('htmlLink'),
                "start_time": start_dt.strftime('%Y-%m-%d %H:%M'),
                "formatted_date": start_dt.strftime('%d %B, %Y'),
                "formatted_time": start_dt.strftime('%I:%M %p'),
                "is_rescheduled": is_rescheduled
            }
        except Exception as e:
            print(f"Error creating calendar event: {e}")
            return {"success": False, "message": str(e)}

calendar_service = CalendarService()
