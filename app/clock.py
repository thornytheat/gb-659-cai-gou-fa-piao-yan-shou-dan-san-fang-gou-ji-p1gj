from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TIMEZONE


def now_iso() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")
