"""Tests for the RSI(2) Connors mean reversion strategy.

Strategy is split into two pure-function pieces so we can test signal logic
without having to hand-compute Wilder smoothing on every test fixture:

    compute_indicators(df)              -> DataFrame[rsi2, sma]
    signals_from_indicators(close, rsi2, sma) -> Series[target_weight]

These tests pin both pieces and the integration via `generate_signals`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.strategies.rsi2_mean_reversion import (
    RSI2MeanReversion,
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


# ---- compute_indicators ----------------------------------------------------

def test_compute_indicators_returns_rsi_and_sma_columns():
    df = _make_df([100.0] * 30)
    out = compute_indicators(df, sma_window=5)
    assert list(out.columns) == ["rsi2", "sma"]
    assert len(out) == len(df)


def test_compute_indicators_rsi_is_between_0_and_100():
    rng = np.random.default_rng(42)
    closes = (100 + rng.normal(0, 1, 50).cumsum()).tolist()
    out = compute_indicators(_make_df(closes), sma_window=5)
    rsi = out["rsi2"].dropna()
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_compute_indicators_sma_warmup_is_nan():
    df = _make_df([100.0] * 30)
    out = compute_indicators(df, sma_window=10)
    assert out["sma"].iloc[:9].isna().all()
    assert not pd.isna(out["sma"].iloc[9])


def test_compute_indicators_rsi_crashes_after_drop():
    # Steady rise then a sharp two-bar drop should push RSI(2) toward 0.
    closes = [100.0] * 20 + [90.0, 88.0]
    out = compute_indicators(_make_df(closes), sma_window=5)
    rsi = out["rsi2"].iloc[-1]
    assert rsi < 10, f"expected RSI(2) < 10 after drops, got {rsi}"


def test_compute_indicators_rsi_spikes_after_rally():
    closes = [100.0] * 20 + [110.0, 112.0]
    out = compute_indicators(_make_df(closes), sma_window=5)
    rsi = out["rsi2"].iloc[-1]
    assert rsi > 90, f"expected RSI(2) > 90 after rallies, got {rsi}"


# ---- signals_from_indicators -----------------------------------------------

def _series(name: str, values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, name=name)


def test_signals_no_entry_when_no_trigger():
    close = _series("close", [100.0] * 10)
    rsi2 = _series("rsi2", [50.0] * 10)
    sma = _series("sma", [80.0] * 10)
    out = signals_from_indicators(close, rsi2, sma)
    assert (out == 0).all()


def test_signals_entry_when_rsi_below_threshold_and_above_sma():
    close = _series("close", [100.0] * 10)
    sma = _series("sma", [80.0] * 10)
    # RSI dips at bar 5
    rsi2 = _series("rsi2", [50, 50, 50, 50, 50, 5, 50, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma)
    assert out.iloc[5] == 1.0


def test_signals_no_entry_when_below_sma():
    # RSI is oversold but trend filter says no — stay flat.
    close = _series("close", [100.0] * 10)
    sma = _series("sma", [120.0] * 10)
    rsi2 = _series("rsi2", [50, 50, 50, 50, 50, 5, 50, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma)
    assert (out == 0).all()


def test_signals_hold_until_rsi_exit():
    close = _series("close", [100.0] * 10)
    sma = _series("sma", [80.0] * 10)
    # Entry bar 2, exit bar 6 when RSI > 70
    rsi2 = _series("rsi2", [50, 50, 5, 30, 40, 50, 80, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma)
    assert out.iloc[0] == 0
    assert out.iloc[1] == 0
    assert out.iloc[2] == 1.0  # entry
    assert (out.iloc[3:6] == 1.0).all()  # hold
    assert out.iloc[6] == 0  # exit triggered by RSI > 70
    assert (out.iloc[7:] == 0).all()


def test_signals_time_exit_after_max_hold_bars():
    close = _series("close", [100.0] * 12)
    sma = _series("sma", [80.0] * 12)
    # Enter at bar 2, RSI stays middling. After 5 bars in position, time-exit.
    rsi2 = _series("rsi2", [50, 50, 5, 40, 40, 40, 40, 40, 50, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma, max_hold_bars=5)
    assert out.iloc[2] == 1.0  # entry
    assert (out.iloc[3:7] == 1.0).all()  # bars 3,4,5,6 = days 1,2,3,4 in position
    assert out.iloc[7] == 0  # bar 7 = 5 days held → exit
    assert (out.iloc[8:] == 0).all()


def test_signals_can_reenter_after_exit():
    close = _series("close", [100.0] * 10)
    sma = _series("sma", [80.0] * 10)
    # Enter bar 1, exit bar 3 (RSI>70), re-enter bar 5 (RSI<10 again)
    rsi2 = _series("rsi2", [50, 5, 50, 80, 50, 5, 50, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma)
    assert out.iloc[1] == 1.0
    assert out.iloc[2] == 1.0
    assert out.iloc[3] == 0
    assert out.iloc[4] == 0
    assert out.iloc[5] == 1.0  # re-entry


def test_signals_no_reentry_in_same_bar_as_exit():
    close = _series("close", [100.0] * 10)
    sma = _series("sma", [80.0] * 10)
    # RSI is 80 (exit) AND <10 simultaneously is impossible, but check the
    # ambiguous case where rsi pops to >70 then back into oversold next bar.
    rsi2 = _series("rsi2", [50, 5, 50, 75, 5, 50, 50, 50, 50, 50])
    out = signals_from_indicators(close, rsi2, sma)
    assert out.iloc[3] == 0   # exit
    assert out.iloc[4] == 1.0  # re-entry on next bar


# ---- generate_signals integration -----------------------------------------

def test_strategy_generate_signals_returns_series_indexed_by_bar_ts():
    strat = RSI2MeanReversion(trend_filter_window=5)
    closes = [100.0] * 20 + [95.0, 94.0, 96.0, 110.0]
    df = _make_df(closes)
    out = strat.generate_signals("SPY", df)
    assert isinstance(out, pd.Series)
    assert (out.index == df.index).all()
    # Values must be in [-1, 1]
    assert out.min() >= -1
    assert out.max() <= 1


def test_strategy_no_lookahead_only_uses_seen_history():
    """generate_signals(symbol, df.iloc[:t+1]) must equal full-call value at bar t."""
    strat = RSI2MeanReversion(trend_filter_window=5)
    rng = np.random.default_rng(7)
    closes = (100 + rng.normal(0, 0.5, 50).cumsum()).tolist()
    df = _make_df(closes)
    full = strat.generate_signals("SPY", df)
    for t in [10, 20, 30, 45]:
        partial = strat.generate_signals("SPY", df.iloc[: t + 1])
        assert partial.iloc[-1] == pytest.approx(full.iloc[t], abs=1e-9), (
            f"look-ahead leak at bar {t}: partial={partial.iloc[-1]} full={full.iloc[t]}"
        )
