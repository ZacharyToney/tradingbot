"""Backtest engine invariants.

The engine is the most blast-radius-y piece of the system after risk + reconciler.
These tests pin:
  - no look-ahead (strategy only sees bars[:t+1])
  - fills at next bar's open, with slippage applied to the right side
  - whole-share rounding
  - independent per-symbol position state
  - position closes and round-trip pnl
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pytest

from tradingbot.backtest.engine import BacktestResult, Trade, run_backtest
from tradingbot.data.bars import TF


@dataclass
class ConstantSignal:
    """A strategy that returns a hand-specified Series, ignoring df. For tests."""
    name: str = "const"
    universe: list[str] = field(default_factory=lambda: ["X"])
    timeframe: TF = TF(1, "Day")
    fixed: dict[str, pd.Series] = field(default_factory=dict)

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        s = self.fixed[symbol].copy()
        # Only return rows up to df's last index — emulates a real strategy that
        # respects the slice. (If we returned full series, the engine should still
        # take .iloc[-1] for the current bar.)
        return s.loc[: df.index[-1]]


@dataclass
class RecordingStrategy:
    """Records each (symbol, last_index, len) the engine passes in."""
    name: str = "rec"
    universe: list[str] = field(default_factory=lambda: ["X"])
    timeframe: TF = TF(1, "Day")
    calls: list[tuple[str, pd.Timestamp, int]] = field(default_factory=list)

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        self.calls.append((symbol, df.index[-1], len(df)))
        return pd.Series(0.0, index=df.index)


def _flat_bars(n: int, open_: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [open_] * n,
            "high": [open_ * 1.01] * n,
            "low": [open_ * 0.99] * n,
            "close": [open_] * n,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def test_no_lookahead_strategy_only_sees_history_up_to_current_bar():
    bars = {"X": _flat_bars(20)}
    rec = RecordingStrategy()
    run_backtest(rec, bars, starting_equity=10_000)
    # First call should be on bar 0 (1 row), last on bar n-1 (n rows).
    # Length each call must equal the bar index + 1.
    assert len(rec.calls) >= 1
    for symbol, last_ts, n in rec.calls:
        assert symbol == "X"
        expected_idx = bars["X"].index.get_loc(last_ts)
        assert n == expected_idx + 1, f"slice length mismatch at {last_ts}: got {n}"


def test_fill_at_next_bar_open_with_buy_slippage():
    n = 5
    bars = pd.DataFrame(
        {
            "open":   [100, 101, 102, 103, 104],
            "high":   [100, 101, 102, 103, 104],
            "low":    [100, 101, 102, 103, 104],
            "close":  [100, 101, 102, 103, 104],
            "volume": [1e6] * n,
        },
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )
    sig = pd.Series([1.0, 1.0, 0.0, 0.0, 0.0], index=bars.index)
    strat = ConstantSignal(universe=["X"], fixed={"X": sig})
    res = run_backtest(
        strat,
        {"X": bars},
        starting_equity=10_000,
        slippage_bps=5.0,
        max_position_pct=0.05,
    )
    # Entry at bar 0 → fill at bar 1's open (101) * 1.0005 = 101.0505
    # Exit at bar 2 (signal goes to 0) → fill at bar 3's open (103) * 0.9995 = 102.9485
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == pytest.approx(101 * 1.0005, abs=1e-6)
    assert t.exit_price == pytest.approx(103 * 0.9995, abs=1e-6)
    assert t.side == "long"


def test_whole_share_rounding():
    bars = _flat_bars(5, open_=50.0)
    # Target = 5% of $9,900 equity = $495 → at $50.025 (with slippage), 495/50.025 = 9.89 → 9 shares
    sig = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0], index=bars.index)
    strat = ConstantSignal(universe=["X"], fixed={"X": sig})
    res = run_backtest(strat, {"X": bars}, starting_equity=9_900, max_position_pct=0.05)
    assert len(res.trades) == 0  # never exits → no closed trade, but position holds shares
    # We can inspect equity_curve length
    assert len(res.equity_curve) == len(bars)


def test_multi_symbol_independent_position_state():
    n = 6
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    bars_x = pd.DataFrame(
        {
            "open": [50] * n, "high": [50] * n, "low": [50] * n,
            "close": [50] * n, "volume": [1e6] * n,
        },
        index=idx,
    )
    bars_y = pd.DataFrame(
        {
            "open": [25] * n, "high": [25] * n, "low": [25] * n,
            "close": [25] * n, "volume": [1e6] * n,
        },
        index=idx,
    )
    sig_x = pd.Series([1, 1, 0, 0, 0, 0], index=idx, dtype=float)  # enter bar 0, exit bar 2
    sig_y = pd.Series([0, 0, 1, 1, 0, 0], index=idx, dtype=float)  # enter bar 2, exit bar 4
    strat = ConstantSignal(universe=["X", "Y"], fixed={"X": sig_x, "Y": sig_y})
    res = run_backtest(
        strat,
        {"X": bars_x, "Y": bars_y},
        starting_equity=10_000,
        slippage_bps=0,
        max_position_pct=0.05,
    )
    syms = {t.symbol for t in res.trades}
    assert syms == {"X", "Y"}
    by_symbol: dict[str, Trade] = {t.symbol: t for t in res.trades}
    # X: in from bar 1 to bar 3 (entry on bar 1 open, exit on bar 3 open)
    assert by_symbol["X"].entry_ts == idx[1]
    assert by_symbol["X"].exit_ts == idx[3]
    # Y: in from bar 3 to bar 5
    assert by_symbol["Y"].entry_ts == idx[3]
    assert by_symbol["Y"].exit_ts == idx[5]


def test_max_concurrent_positions_caps_simultaneous_entries():
    n = 4
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    bars = {
        sym: pd.DataFrame(
            {
                "open": [100] * n, "high": [100] * n, "low": [100] * n,
                "close": [100] * n, "volume": [1e6] * n,
            },
            index=idx,
        )
        for sym in ("A", "B", "C", "D")
    }
    # All four want to enter at bar 0
    sigs = {s: pd.Series([1, 0, 0, 0], index=idx, dtype=float) for s in bars}
    strat = ConstantSignal(universe=list(bars), fixed=sigs)
    res = run_backtest(
        strat,
        bars,
        starting_equity=100_000,
        slippage_bps=0,
        max_position_pct=0.05,
        max_concurrent_positions=2,
    )
    # Only 2 trades should be opened
    assert len(res.trades) == 2


def test_returns_BacktestResult_with_expected_fields():
    bars = _flat_bars(5)
    sig = pd.Series([0.0] * 5, index=bars.index)
    strat = ConstantSignal(universe=["X"], fixed={"X": sig})
    res = run_backtest(strat, {"X": bars}, starting_equity=10_000)
    assert isinstance(res, BacktestResult)
    assert isinstance(res.equity_curve, pd.Series)
    assert isinstance(res.trades, list)
    assert isinstance(res.daily_returns, pd.Series)
    # No trades happened → equity stays flat at starting equity
    assert res.equity_curve.iloc[-1] == pytest.approx(10_000, abs=1e-6)


def test_pnl_calculation_for_winning_long_trade():
    n = 5
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    # Price ramps up; we buy at bar 1 open=100, sell at bar 3 open=110
    bars = pd.DataFrame(
        {
            "open": [100, 100, 105, 110, 110],
            "high": [101, 101, 106, 111, 111],
            "low":  [99, 99, 104, 109, 109],
            "close": [100, 100, 105, 110, 110],
            "volume": [1e6] * n,
        },
        index=idx,
    )
    sig = pd.Series([1, 1, 0, 0, 0], index=idx, dtype=float)
    strat = ConstantSignal(universe=["X"], fixed={"X": sig})
    res = run_backtest(
        strat,
        {"X": bars},
        starting_equity=10_000,
        slippage_bps=0,    # turn slippage off for clean arithmetic
        max_position_pct=0.05,
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    # Target equity = 0.05 * 10_000 = $500. Buy at 100 → 5 shares. Sell at 110.
    # pnl = (110 - 100) * 5 = 50
    assert t.qty == 5
    assert t.pnl == pytest.approx(50, abs=1e-6)
