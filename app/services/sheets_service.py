import json
import asyncio
try:
    import gspread
except ImportError:
    gspread = None
from app.config import settings

class SheetsService:
    def _append_sync(self, s_id: str, token_json_str: str, lead_data: list):
        # Authenticate using gspread with service account credentials json
        creds_data = json.loads(token_json_str)
        gc = gspread.service_account_from_dict(creds_data)
        sh = gc.open_by_key(s_id)
        worksheet = sh.get_worksheet(0) or sh.sheet1
        worksheet.append_row(lead_data)

    async def append_lead(self, spreadsheet_id: str, token_json_str: str, lead_data: list) -> dict:
        s_id = spreadsheet_id or settings.GOOGLE_SHEETS_SPREADSHEET_ID
        if not s_id:
            return {"success": False, "message": "Spreadsheet ID missing."}

        # Auto-extract ID from full Google Sheet URL if necessary
        if "/d/" in s_id:
            try:
                s_id = s_id.split("/d/")[1].split("/")[0]
            except Exception:
                pass

        if not token_json_str or not token_json_str.strip().startswith("{"):
            return {"success": False, "message": "Google Service Account JSON Key missing."}

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._append_sync,
                s_id,
                token_json_str,
                lead_data
            )
            return {"success": True, "message": "Successfully appended lead to Google Sheets"}
        except Exception as e:
            return {"success": False, "message": str(e)}

sheets_service = SheetsService()
