"""Compose `risk/limits.py` checks into a single pre-trade gate.

Order of checks matters: cheap + globally-fatal first (HALT, clock_skew, drawdowns),
then order-specific (sizing, hours, side validity), then PDT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from tradingbot.config import Settings
from tradingbot.risk.limits import (
    AssetClass,
    Decision,
    check_clock_skew,
    check_daily_loss,
    check_equity_drift,
    check_extended_hours,
    check_halt_file,
    check_long_only_equities,
    check_max_concurrent,
    check_max_position_pct,
    check_pdt_violation,
    check_total_drawdown,
)


@dataclass(frozen=True)
class IntendedOrder:
    strategy: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    price: float
    asset_class: AssetClass
    bar_ts_ms: int
    client_order_id: str

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.price


@dataclass(frozen=True)
class AccountState:
    equity_now: float
    equity_start_of_day: float
    equity_all_time_high: float
    last_recon_equity: float | None
    broker_equity: float
    clock_skew_seconds: float
    current_open_positions: int
    current_qty_for_symbol: float
    daytrades_in_5d: int
    now: datetime
    repo_root: Path


def pre_trade(order: IntendedOrder, state: AccountState, settings: Settings) -> Decision:
    """Run checks in order; short-circuit on first failure."""
    checks: list[Decision] = []

    # 1. Globally-fatal short-circuits (do these even on close orders).
    checks.append(check_halt_file(state.repo_root))
    if not checks[-1].allow:
        return checks[-1]

    checks.append(check_clock_skew(state.clock_skew_seconds, settings.clock_skew_max_seconds))
    if not checks[-1].allow:
        return checks[-1]

    checks.append(
        check_daily_loss(state.equity_now, state.equity_start_of_day, settings.daily_loss_limit_pct)
    )
    if not checks[-1].allow:
        return checks[-1]

    checks.append(
        check_total_drawdown(
            state.equity_now, state.equity_all_time_high, settings.total_dd_limit_pct
        )
    )
    if not checks[-1].allow:
        return checks[-1]

    checks.append(
        check_equity_drift(
            state.broker_equity, state.last_recon_equity, settings.equity_drift_max_pct
        )
    )
    if not checks[-1].allow:
        return checks[-1]

    # 2. Order-side validity + timing.
    checks.append(check_extended_hours(state.now, order.asset_class))
    if not checks[-1].allow:
        return checks[-1]

    checks.append(
        check_long_only_equities(
            order.symbol, order.side, order.asset_class, state.current_qty_for_symbol
        )
    )
    if not checks[-1].allow:
        return checks[-1]

    # 3. Size / concurrency. These only apply when opening (or growing) a position.
    is_opening = _is_opening_or_growing(order, state.current_qty_for_symbol)
    if is_opening:
        checks.append(
            check_max_position_pct(order.notional, state.equity_now, settings.max_position_pct)
        )
        if not checks[-1].allow:
            return checks[-1]

        if state.current_qty_for_symbol == 0:
            # opening a brand-new position counts against the concurrent cap
            checks.append(
                check_max_concurrent(
                    state.current_open_positions, settings.max_concurrent_positions
                )
            )
            if not checks[-1].allow:
                return checks[-1]

    # 4. PDT — anywhere in the chain since it's not order-side specific.
    checks.append(check_pdt_violation(state.daytrades_in_5d, state.equity_now))
    if not checks[-1].allow:
        return checks[-1]

    return Decision(True)


def _is_opening_or_growing(order: IntendedOrder, current_qty: float) -> bool:
    """A buy when current_qty >= 0, or a sell when current_qty <= 0, grows the position
    (or opens one). A buy that flattens a short, or a sell that flattens a long, is closing."""
    if order.side == "buy":
        return current_qty >= 0   # adding to long or opening long
    return current_qty <= 0       # adding to short or opening short
