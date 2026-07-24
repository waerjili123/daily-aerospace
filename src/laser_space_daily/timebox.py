"""Beijing-time windows used by daily and rolling intelligence reports."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """Return the current timezone-aware Beijing time."""
    return datetime.now(BEIJING_TIMEZONE)


def _as_beijing_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TIMEZONE)
    return value.astimezone(BEIJING_TIMEZONE)


def daily_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the preceding 24-hour daily window ending at ``now`` in Beijing time."""
    end = _as_beijing_time(now)
    return end - timedelta(days=1), end


def rolling_start(now: datetime) -> datetime:
    """Return the start of the preceding three calendar months in Beijing time."""
    return _as_beijing_time(now) - relativedelta(months=3)
