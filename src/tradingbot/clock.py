from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import ntplib
from loguru import logger

ET = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")


@dataclass(frozen=True)
class ClockCheck:
    ok: bool
    skew_seconds: float
    error: str | None = None


def check_ntp_skew(server: str = "pool.ntp.org", timeout: float = 3.0) -> ClockCheck:
    """Return absolute clock skew vs NTP. ok=False on any failure (be defensive)."""
    try:
        client = ntplib.NTPClient()
        resp = client.request(server, version=3, timeout=timeout)
        skew = abs(resp.offset)
        return ClockCheck(ok=True, skew_seconds=skew)
    except Exception as e:
        logger.warning(f"NTP check failed: {e}")
        return ClockCheck(ok=False, skew_seconds=float("inf"), error=str(e))


def utc_now_ms() -> int:
    return int(time.time() * 1000)


AssetClass = Literal["equity", "crypto"]


def is_us_equity_market_open(now: datetime) -> bool:
    """Mon-Fri 9:30-16:00 ET. Holiday calendar deferred to Alpaca's own rejection."""
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    minute = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minute < 16 * 60


def last_completed_daily_bar_ts(now: datetime, asset_class: AssetClass) -> datetime:
    """Timestamp of the most recent CLOSED daily bar at `now`.

    Equities: Alpaca daily bars are stamped to midnight ET on their trade date.
              Today's bar finalizes at 16:00 ET. Return UTC-midnight of bar_date.
    Crypto:   Bars stamped at start of UTC day; finalize at the NEXT UTC midnight.
              So at any time today, the last finalized bar started YESTERDAY.
    """
    if asset_class == "crypto":
        midnight_today = now.astimezone(UTC_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        # Always the bar that started yesterday and finished at today's UTC midnight.
        return midnight_today - timedelta(days=1)

    # Equities
    et = now.astimezone(ET)
    # If during/after today's session AND today is a weekday: most recent completed bar = today
    # (we treat the bar as "completed" at or after 16:00 ET).
    last_close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    if et >= last_close and et.weekday() < 5:
        bar_date = et.date()
    else:
        # Walk back to previous weekday
        d = et.date()
        while True:
            d = d - timedelta(days=1)
            if d.weekday() < 5:
                break
        bar_date = d
    # Stamp at UTC midnight of the bar date (Alpaca convention).
    return datetime(bar_date.year, bar_date.month, bar_date.day, tzinfo=UTC_TZ)
