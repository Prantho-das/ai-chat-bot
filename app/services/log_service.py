import asyncio
from datetime import datetime
import json
from app.database import AsyncSessionLocal
from app.models import SystemLog

class LogService:
    def __init__(self):
        self._memory_logs = []

    async def log(self, level: str, source: str, message: str, details: str = None):
        log_entry = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level.upper(),
            "source": source,
            "message": message,
            "details": details or ""
        }
        print(f"[{log_entry['timestamp']}] [{log_entry['level']}] [{log_entry['source']}] {log_entry['message']}")
        
        # Keep latest 100 in memory for immediate UI rendering
        self._memory_logs.insert(0, log_entry)
        if len(self._memory_logs) > 100:
            self._memory_logs.pop()

        try:
            async with AsyncSessionLocal() as db:
                db_log = SystemLog(
                    level=level.upper(),
                    source=source,
                    message=message,
                    details=details
                )
                db.add(db_log)
                await db.commit()
        except Exception as e:
            print(f"[LOG SERVICE ERROR] Could not persist log to DB: {e}")

    def get_recent_memory_logs(self, limit: int = 50) -> list[dict]:
        return self._memory_logs[:limit]

log_service = LogService()
