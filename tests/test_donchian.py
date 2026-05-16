"""Donchian breakout (crypto). Long when close breaks above the prior `enter_window`
days' high; exit when close breaks below the prior `exit_window` days' low.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.strategies.donchian_breakout import (
    DonchianBreakout,
    compute_indicators,
    signals_from_indicators,
)


def _make_df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx)


# ---- compute_indicators ----------------------------------------------------

def test_compute_indicators_returns_hi_lo_columns():
    df = _make_df([100.0] * 20)
    out = compute_indicators(df, enter_window=5, exit_window=3)
    assert list(out.columns) == ["hi", "lo"]
    assert len(out) == len(df)


def test_compute_indicators_excludes_current_bar():
    """hi[t] = max(close[t-enter:t]); lo[t] = min(close[t-exit:t]) — NOT including t itself."""
    closes = [10.0, 20.0, 30.0, 40.0, 50.0, 5.0]
    out = compute_indicators(_make_df(closes), enter_window=3, exit_window=3)
    # hi[5] should be max of close[2..4] = max(30,40,50) = 50, not including close[5]=5
    assert out["hi"].iloc[5] == 50.0
    # lo[5] should be min of close[2..4] = 30
    assert out["lo"].iloc[5] == 30.0


def test_compute_indicators_warmup_is_nan():
    df = _make_df([100.0] * 30)
    out = compute_indicators(df, enter_window=20, exit_window=10)
    # First 20 hi's should be NaN; first 10 lo's should be NaN
    assert out["hi"].iloc[:20].isna().all()
    assert not pd.isna(out["hi"].iloc[20])
    assert out["lo"].iloc[:10].isna().all()


# ---- signals_from_indicators -----------------------------------------------

def test_signals_no_entry_when_below_hi():
    close = _series([100.0] * 10)
    hi = _series([200.0] * 10)
    lo = _series([50.0] * 10)
    out = signals_from_indicators(close, hi, lo)
    assert (out == 0).all()


def test_signals_entry_on_upside_break_of_hi():
    close = _series([100, 100, 100, 100, 100, 250, 250])
    hi = _series([200, 200, 200, 200, 200, 200, 200])
    lo = _series([50] * 7)
    out = signals_from_indicators(close, hi, lo)
    assert (out.iloc[:5] == 0).all()
    assert out.iloc[5] == 1.0  # close 250 > hi 200 → entry
    assert out.iloc[6] == 1.0  # hold (close 250 > lo 50)


def test_signals_exit_on_downside_break_of_lo():
    close = _series([100, 100, 250, 250, 250, 40])
    hi = _series([200, 200, 200, 200, 200, 200])
    lo = _series([50, 50, 50, 50, 50, 50])
    out = signals_from_indicators(close, hi, lo)
    assert out.iloc[2] == 1.0  # entry
    assert out.iloc[3] == 1.0  # hold
    assert out.iloc[4] == 1.0  # hold
    assert out.iloc[5] == 0.0  # close 40 < lo 50 → exit


def test_signals_holds_while_in_channel():
    close = _series([100, 250, 240, 230, 220, 210])
    hi = _series([200, 200, 200, 200, 200, 200])
    lo = _series([100, 100, 100, 100, 100, 100])
    out = signals_from_indicators(close, hi, lo)
    assert out.iloc[0] == 0.0
    assert (out.iloc[1:] == 1.0).all()


def test_signals_can_reenter_after_exit():
    close = _series([100, 250, 40, 50, 60, 300])
    hi = _series([200, 200, 200, 200, 200, 200])
    lo = _series([50, 50, 50, 50, 50, 50])
    out = signals_from_indicators(close, hi, lo)
    assert out.iloc[1] == 1.0  # entry
    assert out.iloc[2] == 0.0  # exit (40 < 50)
    assert out.iloc[5] == 1.0  # re-entry (300 > 200)


def test_signals_nan_indicators_no_entry():
    close = _series([100, 200, 300])
    hi = _series([np.nan, np.nan, 100])
    lo = _series([np.nan, np.nan, 50])
    out = signals_from_indicators(close, hi, lo)
    # Bars 0 and 1 are in warmup → must be 0 (no entry on NaN)
    assert out.iloc[0] == 0
    assert out.iloc[1] == 0


# ---- generate_signals integration -----------------------------------------

def test_strategy_no_lookahead_only_uses_history_up_to_t():
    strat = DonchianBreakout(enter_window=5, exit_window=3)
    rng = np.random.default_rng(11)
    closes = (100 + rng.normal(0, 5, 50).cumsum()).tolist()
    df = _make_df(closes)
    full = strat.generate_signals("BTC/USD", df)
    for t in [10, 20, 30, 45]:
        partial = strat.generate_signals("BTC/USD", df.iloc[: t + 1])
        assert partial.iloc[-1] == pytest.approx(full.iloc[t], abs=1e-9), (
            f"look-ahead leak at bar {t}: partial={partial.iloc[-1]} full={full.iloc[t]}"
        )


def test_strategy_universe_is_crypto():
    strat = DonchianBreakout()
    assert all("/" in s for s in strat.universe), strat.universe
