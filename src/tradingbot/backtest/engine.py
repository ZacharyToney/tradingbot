"""Event-driven backtest engine.

Semantics per bar t:
  1. For each symbol: weight = strategy.generate_signals(sym, bars[sym].iloc[:t+1]).iloc[-1]
     We slice the DataFrame BEFORE calling the strategy — no look-ahead.
  2. Determine which symbols should be in/out at bar t+1's open.
  3. If number of intended positions exceeds max_concurrent_positions, drop excess
     (deterministic order: existing positions preserved; new entries dropped
     starting from end of universe list).
  4. At bar t+1's open, fill any needed buys/sells:
       buy fill  = open * (1 + slippage_bps/1e4)
       sell fill = open * (1 - slippage_bps/1e4)
     Qty = floor(target_equity / fill_price).
  5. Mark equity to bar t+1's close.
  6. Record a `Trade` on round-trip (entry → exit).

Cash is not modeled granularly — we assume sized-by-target each position uses
`max_position_pct * starting_equity` (not rolling equity). For v1 this is fine
and matches the "fixed fractional of initial capital" model that's standard
for retail backtests. Compounding can come later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import pandas as pd


class _StrategyLike(Protocol):
    name: str
    universe: list[str]

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series: ...


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: Literal["long", "short"]
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl: float


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade]
    daily_returns: pd.Series
    strategy_name: str = ""


@dataclass
class _OpenPosition:
    symbol: str
    side: Literal["long", "short"]
    entry_ts: pd.Timestamp
    entry_price: float
    qty: float

    def close_pnl(self, exit_price: float) -> float:
        sign = 1.0 if self.side == "long" else -1.0
        return (exit_price - self.entry_price) * self.qty * sign


def _aligned_index(bars: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Use the intersection of all symbols' indices so every bar is comparable."""
    idx: pd.DatetimeIndex | None = None
    for df in bars.values():
        i = df.index
        idx = i if idx is None else idx.intersection(i)
    if idx is None or len(idx) == 0:
        raise ValueError("no aligned bars across symbols")
    return idx.sort_values()


def run_backtest(
    strategy: _StrategyLike,
    bars: dict[str, pd.DataFrame],
    starting_equity: float = 100_000.0,
    slippage_bps: float = 5.0,
    max_position_pct: float = 0.05,
    max_concurrent_positions: int = 3,
) -> BacktestResult:
    if not bars:
        raise ValueError("bars must contain at least one symbol")

    idx = _aligned_index(bars)
    slippage = slippage_bps / 10_000.0
    position_sizing_equity = starting_equity  # see module docstring

    open_positions: dict[str, _OpenPosition] = {}
    trades: list[Trade] = []
    equity_curve: list[float] = []
    equity = starting_equity

    # Walk every aligned bar except the last (we need t+1 to fill at next open).
    for t in range(len(idx)):
        ts = idx[t]

        # 1) Strategy signals for the *current* bar (uses bars[:t+1]).
        intents: dict[str, float] = {}
        for symbol in strategy.universe:
            df = bars.get(symbol)
            if df is None:
                continue
            slice_ = df.loc[: ts]
            if len(slice_) == 0:
                continue
            sigs = strategy.generate_signals(symbol, slice_)
            if len(sigs) == 0:
                continue
            w = float(sigs.iloc[-1])
            if pd.isna(w):
                w = 0.0
            intents[symbol] = max(-1.0, min(1.0, w))

        # 2) Resolve to in/out at bar t+1 open.
        next_t = t + 1
        if next_t >= len(idx):
            # Mark equity to current close and stop — can't fill at t+1.
            equity_curve.append(_mark_to_market(equity, open_positions, bars, ts, mark_at="close"))
            continue
        next_ts = idx[next_t]

        # 2a) Build the "desired" set of symbols with weight != 0.
        # 2b) Respect max_concurrent_positions: keep existing positions, drop excess new entries.
        desired = {s for s, w in intents.items() if w != 0}
        existing = set(open_positions.keys())

        # Always honor exits (existing position with intent 0 → close)
        to_close = [s for s in existing if intents.get(s, 0.0) == 0.0]
        # Prospective new opens (existing positions with non-zero stay)
        new_opens = [s for s in desired if s not in existing]
        capacity = max_concurrent_positions - (len(existing) - len(to_close))
        # Deterministic order: by universe order
        new_opens.sort(key=strategy.universe.index)
        new_opens = new_opens[: max(0, capacity)]

        # 3) Execute closes
        for sym in to_close:
            pos = open_positions.pop(sym)
            next_open = float(bars[sym].loc[next_ts, "open"])
            sell_side_slip = -slippage if pos.side == "long" else slippage
            exit_price = next_open * (1 + sell_side_slip)
            pnl = pos.close_pnl(exit_price)
            equity += pnl
            trades.append(
                Trade(
                    symbol=sym,
                    side=pos.side,
                    entry_ts=pos.entry_ts,
                    exit_ts=next_ts,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    qty=pos.qty,
                    pnl=pnl,
                )
            )

        # 4) Execute opens
        for sym in new_opens:
            w = intents[sym]
            side: Literal["long", "short"] = "long" if w > 0 else "short"
            next_open = float(bars[sym].loc[next_ts, "open"])
            buy_side_slip = slippage if side == "long" else -slippage
            entry_price = next_open * (1 + buy_side_slip)
            target_dollars = position_sizing_equity * max_position_pct
            qty = float(int(target_dollars // entry_price))  # whole shares
            if qty <= 0:
                continue
            open_positions[sym] = _OpenPosition(
                symbol=sym,
                side=side,
                entry_ts=next_ts,
                entry_price=entry_price,
                qty=qty,
            )

        # 5) Mark equity to current bar's close (after t's fills/closes are settled in cash).
        equity_curve.append(_mark_to_market(equity, open_positions, bars, ts, mark_at="close"))

    eq = pd.Series(equity_curve, index=idx[: len(equity_curve)], name="equity")
    daily_returns = eq.pct_change().fillna(0)
    return BacktestResult(
        equity_curve=eq,
        trades=trades,
        daily_returns=daily_returns,
        strategy_name=strategy.name,
    )


def _mark_to_market(
    cash_like_equity: float,
    open_positions: dict[str, _OpenPosition],
    bars: dict[str, pd.DataFrame],
    ts: pd.Timestamp,
    mark_at: Literal["open", "close"] = "close",
) -> float:
    """Equity = realized PnL accumulated into 'cash_like_equity' + unrealized for open positions."""
    unrealized = 0.0
    for pos in open_positions.values():
        df = bars[pos.symbol]
        if ts not in df.index:
            continue
        px = float(df.loc[ts, mark_at])
        sign = 1.0 if pos.side == "long" else -1.0
        unrealized += (px - pos.entry_price) * pos.qty * sign
    return cash_like_equity + unrealized
