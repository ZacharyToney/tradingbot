"""The live loop must NOT pass an in-progress (still-ticking) bar to the strategy.

Alpaca returns intra-day bars for both crypto (stamped at UTC midnight, in progress
all day) and equities (stamped at midnight ET, in progress during RTH). The strategy
is designed for completed bars only — feeding it a half-day's data would produce
look-ahead-like behavior.

These tests pin `_filter_to_complete_bars` (live/loop.py) for both cases.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.clock import last_completed_daily_bar_ts
from tradingbot.live.loop import _filter_to_complete_bars

ET = ZoneInfo("America/New_York")


# ---- last_completed_daily_bar_ts -------------------------------------------

def test_lcb_crypto_returns_yesterday_during_day():
    # Sat 06:00 UTC → last completed = Fri 00:00 UTC (the Friday daily bar)
    now = datetime(2026, 5, 16, 6, 0, tzinfo=UTC)
    bar = last_completed_daily_bar_ts(now, "crypto")
    assert bar == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_lcb_crypto_just_after_midnight():
    # Sun 00:00:01 UTC → last completed = Sat 00:00 UTC
    now = datetime(2026, 5, 17, 0, 0, 1, tzinfo=UTC)
    bar = last_completed_daily_bar_ts(now, "crypto")
    assert bar == datetime(2026, 5, 16, 0, 0, tzinfo=UTC)


def test_lcb_equity_during_rth_returns_prior_weekday():
    # Mon 10:00 ET = 14:00 UTC → Monday's bar is in progress → return Friday's stamp
    now = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    bar = last_completed_daily_bar_ts(now, "equity")
    assert bar == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)  # Friday May 15


def test_lcb_equity_after_close_returns_today():
    # Mon 17:00 ET = 21:00 UTC → today's bar finalized → return today's stamp
    now = datetime(2026, 5, 18, 21, 0, tzinfo=UTC)
    bar = last_completed_daily_bar_ts(now, "equity")
    assert bar == datetime(2026, 5, 18, 0, 0, tzinfo=UTC)


def _df_with_index(stamps: list[str], tz: str | ZoneInfo = "UTC") -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(s, tz=tz) for s in stamps])
    return pd.DataFrame(
        {"open": [1.0] * len(idx), "high": [1.0] * len(idx),
         "low": [1.0] * len(idx), "close": [1.0] * len(idx),
         "volume": [1000] * len(idx)},
        index=idx,
    )


# ---- crypto ----------------------------------------------------------------

def test_crypto_drops_today_bar_when_in_progress():
    # Now is May 16 06:00 UTC (6 hours into May 16's bar).
    now = datetime(2026, 5, 16, 6, 0, tzinfo=UTC)
    df = _df_with_index(["2026-05-14", "2026-05-15", "2026-05-16"])
    out = _filter_to_complete_bars(df, now, "crypto")
    assert len(out) == 2
    assert out.index[-1] == pd.Timestamp("2026-05-15", tz="UTC")


def test_crypto_keeps_today_bar_when_complete():
    # Now is May 17 00:00 UTC exactly — May 16's bar JUST finished.
    now = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    df = _df_with_index(["2026-05-14", "2026-05-15", "2026-05-16"])
    out = _filter_to_complete_bars(df, now, "crypto")
    assert len(out) == 3  # all bars are complete


def test_crypto_empty_df_passes_through():
    now = datetime(2026, 5, 16, 6, 0, tzinfo=UTC)
    df = _df_with_index([])
    out = _filter_to_complete_bars(df, now, "crypto")
    assert len(out) == 0


def test_crypto_only_complete_bars_unaffected():
    # All bars are >= 1 day old. Nothing should be dropped.
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    df = _df_with_index(["2026-05-14", "2026-05-15", "2026-05-16"])
    out = _filter_to_complete_bars(df, now, "crypto")
    assert len(out) == 3


# ---- equity ----------------------------------------------------------------

def test_equity_drops_today_bar_during_rth():
    # Monday 10:00 AM ET = 14:00 UTC (EDT). Today's bar (stamped at midnight ET) is in-progress.
    now = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    df = _df_with_index(
        ["2026-05-14 04:00", "2026-05-15 04:00", "2026-05-18 04:00"], tz="UTC"
    )
    out = _filter_to_complete_bars(df, now, "equity")
    assert len(out) == 2
    assert out.index[-1] == pd.Timestamp("2026-05-15 04:00", tz="UTC")


def test_equity_keeps_today_bar_after_close():
    # Monday 16:01 ET = 20:01 UTC (EDT). Today's bar is now complete.
    now = datetime(2026, 5, 18, 20, 1, tzinfo=UTC)
    df = _df_with_index(
        ["2026-05-14 04:00", "2026-05-15 04:00", "2026-05-18 04:00"], tz="UTC"
    )
    out = _filter_to_complete_bars(df, now, "equity")
    assert len(out) == 3


def test_equity_weekend_keeps_all():
    # Saturday — Friday's bar is the latest, fully complete.
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    df = _df_with_index(["2026-05-14 04:00", "2026-05-15 04:00"], tz="UTC")
    out = _filter_to_complete_bars(df, now, "equity")
    assert len(out) == 2


def test_equity_premarket_drops_today():
    # Monday 08:00 ET = 12:00 UTC (EDT) — pre-market, today's bar still in-progress.
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    df = _df_with_index(
        ["2026-05-14 04:00", "2026-05-15 04:00", "2026-05-18 04:00"], tz="UTC"
    )
    out = _filter_to_complete_bars(df, now, "equity")
    assert len(out) == 2
