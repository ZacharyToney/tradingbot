"""Chaos Crypto Momentum: aggressive 1h Donchian breakout with 24h time stop.

Strategy is intentionally similar to donchian_breakout but on shorter windows and
with an added time stop. These tests pin the time-stop behavior specifically since
it's the new wrinkle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.strategies.chaos_crypto import (
    ChaosCryptoMomentum,
    compute_indicators,
    signals_from_indicators,
)


def _make_df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="1h")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100_000] * len(closes),
        },
        index=idx,
    )


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="1h")
    return pd.Series(values, index=idx)


def test_compute_indicators_uses_strictly_prior_bars():
    """hi[t] = max(close[t-enter:t]) — current bar excluded."""
    closes = [10, 20, 30, 40, 50, 5]
    out = compute_indicators(_make_df(closes), enter_window=3, exit_window=3)
    # hi[5] = max of close[2..4] = 50, not 5
    assert out["hi"].iloc[5] == 50.0
    assert out["lo"].iloc[5] == 30.0


def test_signals_entry_and_low_exit():
    close = _series([100, 100, 100, 250, 250, 40])
    hi = _series([200, 200, 200, 200, 200, 200])
    lo = _series([50, 50, 50, 50, 50, 50])
    out = signals_from_indicators(close, hi, lo, time_stop_bars=24)
    assert out.iloc[2] == 0.0   # below hi
    assert out.iloc[3] == 1.0   # entry (250 > 200)
    assert out.iloc[4] == 1.0   # hold
    assert out.iloc[5] == 0.0   # low exit (40 < 50)


def test_time_stop_exits_after_n_bars_even_without_low_break():
    """Position should close at exactly `time_stop_bars` bars after entry even if
    the low never breaks. After the time stop, close drifts back below the breakout
    threshold so we don't immediately re-enter (re-entry is tested separately)."""
    # Entry bar at index 2, then hold flat (between hi and lo). With time_stop=3,
    # the 3rd bar after entry (index 5) should exit. After that, close is below
    # hi so no re-entry.
    close = _series([100, 100, 250, 240, 230, 220, 150, 140])
    hi = _series([200] * 8)
    lo = _series([50] * 8)
    out = signals_from_indicators(close, hi, lo, time_stop_bars=3)
    assert out.iloc[1] == 0.0   # no entry yet
    assert out.iloc[2] == 1.0   # entry
    assert out.iloc[3] == 1.0   # hold bar 1
    assert out.iloc[4] == 1.0   # hold bar 2
    assert out.iloc[5] == 0.0   # TIME STOP at bar 3
    assert out.iloc[6] == 0.0   # flat (close 150 < hi 200, no re-entry)


def test_time_stop_does_not_fire_if_low_exit_happens_first():
    """If the low exit triggers before the time stop, that takes precedence — the
    bar count resets to zero and we don't double-exit."""
    close = _series([100, 250, 250, 40, 50, 60])
    hi = _series([200] * 6)
    lo = _series([50] * 6)
    out = signals_from_indicators(close, hi, lo, time_stop_bars=10)
    assert out.iloc[1] == 1.0  # entry
    assert out.iloc[2] == 1.0  # hold
    assert out.iloc[3] == 0.0  # low exit (40 < 50), NOT time stop
    assert out.iloc[4] == 0.0  # flat
    # No phantom re-entry on bar 4 or 5 just because counter reset.


def test_time_stop_resets_on_reentry():
    """After exit, a new entry resets the time-stop counter."""
    close = _series([100, 250, 40, 50, 300, 290, 280, 270])
    hi = _series([200] * 8)
    lo = _series([50] * 8)
    out = signals_from_indicators(close, hi, lo, time_stop_bars=3)
    assert out.iloc[1] == 1.0  # first entry
    assert out.iloc[2] == 0.0  # low exit
    assert out.iloc[4] == 1.0  # re-entry on 300 > 200
    assert out.iloc[5] == 1.0  # hold 1
    assert out.iloc[6] == 1.0  # hold 2
    assert out.iloc[7] == 0.0  # time stop at bar 3


def test_signals_nan_indicators_no_entry():
    close = _series([100, 200, 300])
    hi = _series([np.nan, np.nan, 100])
    lo = _series([np.nan, np.nan, 50])
    out = signals_from_indicators(close, hi, lo)
    assert out.iloc[0] == 0
    assert out.iloc[1] == 0


def test_strategy_no_lookahead():
    strat = ChaosCryptoMomentum(enter_window=4, exit_window=2, time_stop_bars=10)
    rng = np.random.default_rng(42)
    closes = (100 + rng.normal(0, 5, 40).cumsum()).tolist()
    df = _make_df(closes)
    full = strat.generate_signals("BTC/USD", df)
    for t in [10, 20, 30, 35]:
        partial = strat.generate_signals("BTC/USD", df.iloc[: t + 1])
        assert partial.iloc[-1] == pytest.approx(full.iloc[t], abs=1e-9), (
            f"look-ahead leak at bar {t}: partial={partial.iloc[-1]} full={full.iloc[t]}"
        )


def test_strategy_universe_is_crypto():
    strat = ChaosCryptoMomentum()
    assert all("/" in s for s in strat.universe), strat.universe


def test_strategy_timeframe_is_1h():
    strat = ChaosCryptoMomentum()
    assert strat.timeframe.amount == 1
    assert strat.timeframe.unit == "Hour"
