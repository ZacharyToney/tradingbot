"""Walk-forward parameter optimization.

For each rolling (train, test) window:
  1. Cartesian-product the parameter grid.
  2. For each combo: instantiate the strategy with those params, run our event-driven
     backtest on the TRAIN window only, score by `objective` (sharpe/cagr/profit_factor).
  3. Pick the best-scoring combo. Re-run that combo's strategy on the TEST window
     (out-of-sample). Record train + test metrics + chosen params.
  4. Roll forward by `roll_months`, repeat.

Final summary: mean / std of out-of-sample metrics. If OOS metrics degrade > 50% vs IS,
flag the strategy as likely overfit.
"""
from __future__ import annotations

import csv
import itertools
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from tradingbot.backtest.engine import run_backtest
from tradingbot.backtest.metrics import Metrics, compute_metrics


@dataclass(frozen=True)
class ParamGrid:
    strategy: str
    grid: dict[str, list]


@dataclass(frozen=True)
class WindowResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict
    train_metrics: Metrics
    test_metrics: Metrics


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[WindowResult]
    objective: str

    def overfit_flag(self, threshold: float = 0.5) -> bool:
        """True if OOS objective is below `threshold * IS` on average."""
        if not self.windows:
            return False
        is_vals = [_score(w.train_metrics, self.objective) for w in self.windows]
        oos_vals = [_score(w.test_metrics, self.objective) for w in self.windows]
        is_mean = sum(v for v in is_vals if math.isfinite(v)) / max(1, len(is_vals))
        oos_mean = sum(v for v in oos_vals if math.isfinite(v)) / max(1, len(oos_vals))
        if is_mean <= 0:
            return False
        return oos_mean < threshold * is_mean


def expand_grid(grid: ParamGrid) -> Iterator[dict]:
    keys = list(grid.grid.keys())
    for combo in itertools.product(*(grid.grid[k] for k in keys)):
        yield dict(zip(keys, combo, strict=True))


def iter_windows(
    start: pd.Timestamp,
    end: pd.Timestamp,
    train_months: int,
    test_months: int,
    roll_months: int,
) -> Iterator[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Yield (train_start, train_end, test_start, test_end) tuples until we run out of history."""
    cur = start
    while True:
        train_start = cur
        train_end = cur + pd.DateOffset(months=train_months)
        test_start = train_end
        test_end = test_start + pd.DateOffset(months=test_months)
        if test_end > end:
            return
        yield (train_start, train_end, test_start, test_end)
        cur = cur + pd.DateOffset(months=roll_months)


def _slice_bars(
    bars: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
) -> dict[str, pd.DataFrame]:
    """Slice each symbol's bars to [start, end). Drop symbols with no rows in the window."""
    out: dict[str, pd.DataFrame] = {}
    for sym, df in bars.items():
        idx = df.index
        if idx.tz is None and start.tz is not None:
            start_, end_ = start.tz_localize(None), end.tz_localize(None)
        else:
            start_, end_ = start, end
        sliced = df.loc[(idx >= start_) & (idx < end_)]
        if len(sliced) > 0:
            out[sym] = sliced
    return out


def _score(metrics: Metrics, objective: str) -> float:
    if objective == "sharpe":
        return metrics.sharpe
    if objective == "cagr":
        return metrics.cagr
    if objective == "profit_factor":
        pf = metrics.profit_factor
        return pf if math.isfinite(pf) else 0.0
    raise ValueError(f"unknown objective: {objective}")


def walk_forward(
    strategy_factory: Callable[..., object],
    grid: ParamGrid,
    bars: dict[str, pd.DataFrame],
    train_months: int,
    test_months: int,
    roll_months: int,
    objective: Literal["sharpe", "cagr", "profit_factor"] = "sharpe",
    starting_equity: float = 100_000.0,
    slippage_bps: float = 5.0,
    max_position_pct: float = 0.05,
    max_concurrent_positions: int = 3,
) -> WalkForwardResult:
    # Determine combined history bounds from the bars.
    starts = [df.index.min() for df in bars.values() if len(df)]
    ends = [df.index.max() for df in bars.values() if len(df)]
    if not starts:
        return WalkForwardResult(windows=[], objective=objective)
    start = max(starts)
    end = min(ends)

    windows: list[WindowResult] = []
    for tr_start, tr_end, te_start, te_end in iter_windows(
        start, end, train_months, test_months, roll_months
    ):
        train_bars = _slice_bars(bars, tr_start, tr_end)
        test_bars = _slice_bars(bars, te_start, te_end)
        # Need bars in BOTH train and test slices to evaluate the window.
        if not train_bars or not test_bars:
            continue

        # Optimize on train window.
        best_score = -math.inf
        best_params: dict = {}
        best_train_metrics: Metrics | None = None
        for params in expand_grid(grid):
            strategy = strategy_factory(**params)
            result = run_backtest(
                strategy,
                train_bars,
                starting_equity=starting_equity,
                slippage_bps=slippage_bps,
                max_position_pct=max_position_pct,
                max_concurrent_positions=max_concurrent_positions,
            )
            m = compute_metrics(result.equity_curve, result.trades)
            score = _score(m, objective)
            if score > best_score:
                best_score = score
                best_params = params
                best_train_metrics = m

        # Evaluate best params on the test window (out-of-sample).
        strategy = strategy_factory(**best_params)
        test_result = run_backtest(
            strategy,
            test_bars,
            starting_equity=starting_equity,
            slippage_bps=slippage_bps,
            max_position_pct=max_position_pct,
            max_concurrent_positions=max_concurrent_positions,
        )
        test_metrics = compute_metrics(test_result.equity_curve, test_result.trades)

        windows.append(
            WindowResult(
                train_start=tr_start,
                train_end=tr_end,
                test_start=te_start,
                test_end=te_end,
                best_params=best_params,
                train_metrics=best_train_metrics or test_metrics,
                test_metrics=test_metrics,
            )
        )

    return WalkForwardResult(windows=windows, objective=objective)


def write_report(result: WalkForwardResult, out_root: Path, extra: dict[str, str]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_root / f"{extra.get('strategy', 'strategy')}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # windows.csv
    with (out_dir / "windows.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "train_start", "train_end", "test_start", "test_end",
                "best_params",
                "is_sharpe", "is_max_dd", "is_cagr", "is_win_rate", "is_trade_count",
                "oos_sharpe", "oos_max_dd", "oos_cagr", "oos_win_rate", "oos_trade_count",
            ]
        )
        for win in result.windows:
            w.writerow(
                [
                    win.train_start.isoformat(),
                    win.train_end.isoformat(),
                    win.test_start.isoformat(),
                    win.test_end.isoformat(),
                    str(win.best_params),
                    f"{win.train_metrics.sharpe:.4f}",
                    f"{win.train_metrics.max_dd:.4f}",
                    f"{win.train_metrics.cagr:.4f}",
                    f"{win.train_metrics.win_rate:.4f}",
                    win.train_metrics.trade_count,
                    f"{win.test_metrics.sharpe:.4f}",
                    f"{win.test_metrics.max_dd:.4f}",
                    f"{win.test_metrics.cagr:.4f}",
                    f"{win.test_metrics.win_rate:.4f}",
                    win.test_metrics.trade_count,
                ]
            )

    # summary.txt
    lines: list[str] = [f"strategy: {extra.get('strategy', '?')}"]
    for k, v in extra.items():
        if k != "strategy":
            lines.append(f"{k}: {v}")
    lines.append(f"windows: {len(result.windows)}")
    if result.windows:
        is_sharpe = [w.train_metrics.sharpe for w in result.windows]
        oos_sharpe = [w.test_metrics.sharpe for w in result.windows]
        lines.append("")
        lines.append(f"  IS  Sharpe mean: {sum(is_sharpe)/len(is_sharpe):.4f}")
        lines.append(f"  OOS Sharpe mean: {sum(oos_sharpe)/len(oos_sharpe):.4f}")
        lines.append(
            f"  overfit_flag (OOS<50% of IS): {result.overfit_flag()}"
        )
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")
    return out_dir
