from __future__ import annotations

import time
from dataclasses import dataclass

import ntplib
from loguru import logger


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
