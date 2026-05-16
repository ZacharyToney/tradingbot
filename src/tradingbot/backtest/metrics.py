from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from tradingbot.backtest.engine import Trade


@dataclass(frozen=True)
class Metrics:
    sharpe: float
    sortino: float
    max_dd: float
    cagr: float
    win_rate: float
    profit_factor: float
    trade_count: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_dd": self.max_dd,
            "cagr": self.cagr,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "trade_count": self.trade_count,
        }


def compute_metrics(equity_curve: pd.Series, trades: list[Trade]) -> Metrics:
    eq = equity_curve.astype(float).dropna()
    if len(eq) < 2:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, len(trades))

    returns = eq.pct_change().dropna()

    sharpe = _annualized_sharpe(returns)
    sortino = _annualized_sortino(returns)
    max_dd = _max_drawdown(eq)
    cagr = _cagr(eq)
    win_rate, profit_factor = _trade_stats(trades)

    return Metrics(
        sharpe=sharpe,
        sortino=sortino,
        max_dd=max_dd,
        cagr=cagr,
        win_rate=win_rate,
        profit_factor=profit_factor,
        trade_count=len(trades),
    )


def _annualized_sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    if len(returns) == 0:
        return 0.0
    std = returns.std()
    if std == 0 or math.isnan(std):
        return 0.0
    return float(math.sqrt(periods_per_year) * returns.mean() / std)


def _annualized_sortino(returns: pd.Series, periods_per_year: int = 252) -> float:
    if len(returns) == 0:
        return 0.0
    downside = returns[returns < 0]
    dd_std = downside.std()
    if len(downside) == 0 or dd_std == 0 or math.isnan(dd_std):
        return 0.0
    return float(math.sqrt(periods_per_year) * returns.mean() / dd_std)


def _max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def _cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    total_return = equity.iloc[-1] / equity.iloc[0]
    years = len(equity) / periods_per_year
    if years <= 0:
        return 0.0
    return float(total_return ** (1 / years) - 1)


def _trade_stats(trades: list[Trade]) -> tuple[float, float]:
    if not trades:
        return 0.0, 0.0
    winners = [t.pnl for t in trades if t.pnl > 0]
    losers = [t.pnl for t in trades if t.pnl < 0]
    win_rate = len(winners) / len(trades)
    if not losers:
        # All winners (or no losers) → undefined; return +inf so downstream tests can pin it.
        return win_rate, math.inf if winners else 0.0
    profit_factor = sum(winners) / abs(sum(losers))
    return win_rate, profit_factor
