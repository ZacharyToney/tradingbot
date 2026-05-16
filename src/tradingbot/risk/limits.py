"""Circuit breakers. Each function returns a `Decision`. Composed by `gates.pre_trade`.

Defensive design: a check failure means *don't trade*. Errors and unknown states fail closed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

AssetClass = Literal["equity", "crypto"]

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


def check_max_position_pct(order_value: float, equity: float, max_pct: float) -> Decision:
    if equity <= 0:
        return Decision(False, "max_position_pct: zero/negative equity")
    pct = order_value / equity
    # Allow ~0.01% relative tolerance for float roundoff in `qty * price` calculations
    # (sized exactly at cap can drift slightly above due to FP arithmetic).
    if pct > max_pct * 1.0001:
        return Decision(
            False,
            f"max_position_pct: order {pct:.6f} > cap {max_pct:.6f}",
        )
    return Decision(True)


def check_max_concurrent(current_open: int, max_concurrent: int) -> Decision:
    if current_open >= max_concurrent:
        return Decision(
            False, f"max_concurrent: already {current_open} open, cap {max_concurrent}"
        )
    return Decision(True)


def check_daily_loss(
    equity_now: float, equity_start_of_day: float, limit_pct: float
) -> Decision:
    if equity_start_of_day <= 0:
        return Decision(False, "daily_loss: invalid start-of-day equity")
    loss_pct = (equity_start_of_day - equity_now) / equity_start_of_day
    if loss_pct >= limit_pct - 1e-12:
        return Decision(
            False,
            f"daily_loss: {loss_pct:.4f} >= cap {limit_pct:.4f}",
        )
    return Decision(True)


def check_total_drawdown(
    equity_now: float, equity_all_time_high: float, limit_pct: float
) -> Decision:
    if equity_all_time_high <= 0:
        return Decision(False, "total_drawdown: invalid all-time high")
    dd_pct = (equity_all_time_high - equity_now) / equity_all_time_high
    if dd_pct >= limit_pct - 1e-12:
        return Decision(
            False, f"total_drawdown: {dd_pct:.4f} >= cap {limit_pct:.4f}"
        )
    return Decision(True)


def check_clock_skew(skew_seconds: float, max_skew_seconds: int | float) -> Decision:
    if math.isnan(skew_seconds) or math.isinf(skew_seconds):
        return Decision(False, "clock_skew: NTP probe failed (skew unknown)")
    if abs(skew_seconds) > max_skew_seconds:
        return Decision(
            False,
            f"clock_skew: {abs(skew_seconds):.2f}s > cap {max_skew_seconds}s",
        )
    return Decision(True)


def check_equity_drift(
    broker_equity: float,
    last_recon_equity: float | None,
    max_drift_pct: float,
) -> Decision:
    if last_recon_equity is None:
        return Decision(True)  # first run — no prior snapshot to drift from
    if last_recon_equity <= 0:
        return Decision(False, "equity_drift: invalid prior snapshot")
    drift = abs(broker_equity - last_recon_equity) / last_recon_equity
    if drift > max_drift_pct + 1e-12:
        return Decision(
            False,
            f"equity_drift: {drift:.4f} > cap {max_drift_pct:.4f} "
            f"(broker={broker_equity:.2f}, recon={last_recon_equity:.2f})",
        )
    return Decision(True)


def check_pdt_violation(daytrades_in_5d: int, equity: float) -> Decision:
    """PDT rule: < $25k equity + 4+ day-trades in rolling 5 business days = flagged.
    We refuse the 4th day-trade to stay below the threshold."""
    if equity >= 25_000.0:
        return Decision(True)
    if daytrades_in_5d >= 4:
        return Decision(
            False,
            f"pdt: equity {equity:.2f} < $25k and day-trade count {daytrades_in_5d} >= 4",
        )
    return Decision(True)


def check_halt_file(repo_root: Path) -> Decision:
    if (repo_root / "HALT").exists():
        return Decision(False, "halt_file: ./HALT present")
    return Decision(True)


def check_extended_hours(now: datetime, asset_class: AssetClass) -> Decision:
    if asset_class == "crypto":
        return Decision(True)
    # Convert to ET, check weekday + 9:30-16:00
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return Decision(False, f"extended_hours: weekend ({et.strftime('%A')})")
    open_minute = 9 * 60 + 30
    close_minute = 16 * 60
    cur_minute = et.hour * 60 + et.minute
    if not (open_minute <= cur_minute < close_minute):
        return Decision(
            False, f"extended_hours: ET {et.strftime('%H:%M')} outside 09:30-16:00 RTH"
        )
    return Decision(True)


def check_long_only_equities(
    symbol: str,
    side: str,                  # "buy" | "sell"
    asset_class: AssetClass,
    current_qty: float = 0.0,
) -> Decision:
    if asset_class != "equity":
        return Decision(True)
    if side == "buy":
        return Decision(True)
    # side == "sell"
    if current_qty > 0:
        return Decision(True)  # closing an existing long is fine
    return Decision(
        False,
        f"long-only: {symbol} sell with no existing long would open a short",
    )
