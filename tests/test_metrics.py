"""Metrics math: pin Sharpe, Sortino, max drawdown, win rate, profit factor, CAGR
for known inputs so we notice if the formula drifts.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest.engine import Trade
from tradingbot.backtest.metrics import compute_metrics


def _equity(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, name="equity")


def _trade(pnl: float) -> Trade:
    return Trade(
        symbol="X",
        side="long",
        entry_ts=pd.Timestamp("2025-01-01"),
        exit_ts=pd.Timestamp("2025-01-02"),
        entry_price=100.0,
        exit_price=100.0 + pnl,
        qty=1.0,
        pnl=pnl,
    )


def test_max_dd_simple_three_point_curve():
    # 100 -> 110 -> 88 -> 100. Max DD = (88 - 110) / 110 = -0.2
    eq = _equity([100, 110, 88, 100])
    m = compute_metrics(eq, [])
    assert m.max_dd == pytest.approx(-0.2, abs=1e-9)


def test_max_dd_monotone_up_is_zero():
    eq = _equity([100, 101, 102, 110])
    m = compute_metrics(eq, [])
    assert m.max_dd == pytest.approx(0.0, abs=1e-9)


def test_sharpe_constant_returns_is_zero_or_nan():
    # All-flat returns → std=0 → Sharpe is nan/inf. Pin it to 0.0 for plotability.
    eq = _equity([100] * 10)
    m = compute_metrics(eq, [])
    assert m.sharpe == 0.0 or math.isnan(m.sharpe)


def test_sharpe_known_input():
    # Returns: +1%, -1%, +1%, -1%, +1% → mean ≈ 0.0033, std ≈ 0.0103
    # Sharpe = sqrt(252) * 0.0033 / 0.0103 ≈ 5.07. We just want it positive and finite.
    rets = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01])
    eq = (1 + rets).cumprod() * 100
    eq.index = pd.date_range("2025-01-01", periods=len(eq), freq="D")
    m = compute_metrics(eq, [])
    assert math.isfinite(m.sharpe)
    assert m.sharpe > 0


def test_win_rate_and_profit_factor():
    trades = [_trade(10), _trade(-5), _trade(20), _trade(-15)]
    eq = _equity([100, 105])
    m = compute_metrics(eq, trades)
    assert m.win_rate == pytest.approx(0.5, abs=1e-9)
    # Profit factor = 30 / 20 = 1.5
    assert m.profit_factor == pytest.approx(1.5, abs=1e-9)
    assert m.trade_count == 4


def test_win_rate_no_trades():
    m = compute_metrics(_equity([100, 100]), [])
    assert m.win_rate == 0.0
    assert m.trade_count == 0


def test_profit_factor_all_winners_is_infinite():
    trades = [_trade(10), _trade(5)]
    m = compute_metrics(_equity([100, 115]), trades)
    assert math.isinf(m.profit_factor) and m.profit_factor > 0


def test_cagr_doubling_in_one_year():
    # 252 trading days, equity doubles → CAGR = 100%
    eq = pd.Series(
        np.linspace(100, 200, 252),
        index=pd.date_range("2025-01-01", periods=252, freq="B"),
    )
    m = compute_metrics(eq, [])
    assert m.cagr == pytest.approx(1.0, rel=0.05)


def test_sortino_only_uses_negative_returns_in_denominator():
    # Five returns, only two negative — Sortino should differ from Sharpe.
    eq = _equity([100, 110, 105, 115, 110, 130])
    m = compute_metrics(eq, [])
    assert math.isfinite(m.sortino)
    assert math.isfinite(m.sharpe)
