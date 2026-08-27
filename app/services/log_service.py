import asyncio
from datetime import datetime
import json
from app.database import AsyncSessionLocal
from app.models import SystemLog

class LogService:
    def __init__(self):
        self._memory_logs = []

    async def log(self, *args, **kwargs):
        # Flexible signature parser for:
        # 1. log(level, source, message, details=None)
        # 2. log(db, source, level, message, details=None)
        # 3. log(source=..., level=..., message=..., details=...)
        level = kwargs.get("level")
        source = kwargs.get("source")
        message = kwargs.get("message")
        details = kwargs.get("details", "")

        pos_args = list(args)
        if pos_args:
            # Check if first arg is a DB session
            if hasattr(pos_args[0], "execute") or hasattr(pos_args[0], "commit"):
                pos_args.pop(0)
            
            if len(pos_args) >= 3:
                # Could be (level, source, message) or (source, level, message)
                first, second, third = pos_args[0], pos_args[1], pos_args[2]
                fourth = pos_args[3] if len(pos_args) > 3 else details
                
                valid_levels = {"INFO", "SUCCESS", "WARNING", "ERROR", "DEBUG"}
                if str(first).upper() in valid_levels:
                    level = level or str(first).upper()
                    source = source or str(second)
                    message = message or str(third)
                else:
                    source = source or str(first)
                    level = level or (str(second).upper() if str(second).upper() in valid_levels else "INFO")
                    message = message or str(third)
                details = fourth
            elif len(pos_args) == 2:
                level = level or "INFO"
                source = source or str(pos_args[0])
                message = message or str(pos_args[1])
            elif len(pos_args) == 1:
                level = level or "INFO"
                source = source or "System"
                message = message or str(pos_args[0])

        level = (level or "INFO").upper()
        source = source or "System"
        message = message or ""
        details = details or ""

        log_entry = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "source": source,
            "message": message,
            "details": details
        }
        print(f"[{log_entry['timestamp']}] [{log_entry['level']}] [{log_entry['source']}] {log_entry['message']}")
        
        # Keep latest 100 in memory for immediate UI rendering
        self._memory_logs.insert(0, log_entry)
        if len(self._memory_logs) > 100:
            self._memory_logs.pop()

        try:
            async with AsyncSessionLocal() as db:
                db_log = SystemLog(
                    level=level,
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
