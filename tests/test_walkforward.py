"""Walk-forward validates that parameter optimization isn't just curve-fitting:
  - Slice history into rolling (train, test) windows
  - Pick best params on each train slice
  - Evaluate those params on the out-of-sample test slice
  - Roll forward by N months, repeat

If out-of-sample Sharpe is dramatically worse than in-sample, the strategy is overfit.
"""
from __future__ import annotations

import pandas as pd

from tradingbot.backtest.walkforward import (
    ParamGrid,
    expand_grid,
    iter_windows,
    walk_forward,
)


def test_expand_grid_cartesian_product():
    grid = ParamGrid(
        strategy="rsi2",
        grid={"a": [1, 2], "b": [10, 20, 30]},
    )
    combos = list(expand_grid(grid))
    assert len(combos) == 6
    # Order: a=1/b=10, a=1/b=20, a=1/b=30, a=2/b=10, a=2/b=20, a=2/b=30
    assert combos[0] == {"a": 1, "b": 10}
    assert combos[-1] == {"a": 2, "b": 30}


def test_expand_grid_single_value():
    grid = ParamGrid(strategy="rsi2", grid={"a": [42]})
    combos = list(expand_grid(grid))
    assert combos == [{"a": 42}]


def test_iter_windows_counts_correctly():
    # 36 months of history, train=24, test=6, roll=6 → windows at months [0..30, 6..36] = 2 windows
    start = pd.Timestamp("2020-01-01", tz="UTC")
    end = pd.Timestamp("2023-01-01", tz="UTC")
    windows = list(iter_windows(start, end, train_months=24, test_months=6, roll_months=6))
    # 36 months total. (train=24 + test=6) = 30 months/window. First window ends at month 30.
    # Roll by 6 → next would start at month 6, end at month 36. So 2 windows.
    assert len(windows) == 2
    # First window: train [Jan 2020 .. Jan 2022), test [Jan 2022 .. Jul 2022)
    train_start, train_end, test_start, test_end = windows[0]
    assert train_start == start
    assert (train_end.year, train_end.month) == (2022, 1)
    assert (test_end.year, test_end.month) == (2022, 7)


def test_iter_windows_handles_undersized_history():
    """Less than train+test months → no windows produced."""
    start = pd.Timestamp("2024-01-01", tz="UTC")
    end = pd.Timestamp("2024-06-01", tz="UTC")
    windows = list(iter_windows(start, end, train_months=24, test_months=6, roll_months=6))
    assert windows == []


def test_walk_forward_smoke_runs_end_to_end():
    """Use a trivial constant-signal strategy + synthetic bars to verify the orchestration."""
    from dataclasses import dataclass, field

    from tradingbot.data.bars import TF

    @dataclass(frozen=True)
    class ConstStrategy:
        # `theta` is a param we'll tune over
        theta: float = 0.0
        name: str = "const"
        timeframe: TF = TF(1, "Day")
        universe_v: list[str] = field(default_factory=lambda: ["X"])

        @property
        def universe(self) -> list[str]:
            return self.universe_v

        def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
            # Always flat — produces a 0-trade backtest, but the walk-forward orchestration
            # should still execute cleanly across windows.
            return pd.Series(0.0, index=df.index)

    # 3 years of synthetic flat bars
    idx = pd.date_range("2020-01-01", "2023-01-01", freq="D", tz="UTC")
    bars = {
        "X": pd.DataFrame(
            {
                "open": [100.0] * len(idx),
                "high": [101.0] * len(idx),
                "low": [99.0] * len(idx),
                "close": [100.0] * len(idx),
                "volume": [1_000_000] * len(idx),
            },
            index=idx,
        )
    }

    grid = ParamGrid(strategy="const", grid={"theta": [0.0, 0.1]})
    result = walk_forward(
        strategy_factory=ConstStrategy,
        grid=grid,
        bars=bars,
        train_months=24,
        test_months=6,
        roll_months=6,
        objective="sharpe",
    )
    # Should have ≥1 window, with chosen_params present.
    assert len(result.windows) >= 1
    for w in result.windows:
        assert "theta" in w.best_params
        assert w.train_metrics is not None
        assert w.test_metrics is not None
