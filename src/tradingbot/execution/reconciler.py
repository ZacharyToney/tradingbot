"""Diff a strategy's target weights against bot-managed positions; emit minimal orders.

The reconciler is intentionally pure: no broker calls, no DB writes. It takes snapshots
and returns a list of `IntendedOrder`s for the runner to gate and submit.
"""
from __future__ import annotations

import uuid

from tradingbot.risk.gates import IntendedOrder
from tradingbot.risk.limits import AssetClass
from tradingbot.risk.sizing import size_position

# Namespace for uuid5 client_order_id derivation. Constant for the project's lifetime —
# changing it invalidates idempotency across all historical orders.
_CID_NAMESPACE = uuid.UUID("a4f0d2b1-77c8-4f0e-9a4f-8b1d2e3f4a5b")

# Don't emit an order for diffs smaller than 1 share (equities) — matches the whole-share
# rounding in `size_position`. Crypto dust is filtered upstream in `size_position`.
_DUST_QTY_EPS = 1.0


def _build_client_order_id(strategy: str, symbol: str, bar_ts_ms: int, side: str) -> str:
    seed = f"{strategy}|{symbol}|{bar_ts_ms}|{side}"
    return f"{strategy[:10]}-{uuid.uuid5(_CID_NAMESPACE, seed)}"


def reconcile(
    targets: dict[str, float],
    bot_positions: dict[str, float],
    prices: dict[str, float],
    equity_for_sizing: float,
    max_position_pct: float,
    asset_classes: dict[str, AssetClass],
    strategy_name: str,
    bar_ts_ms: int,
) -> list[IntendedOrder]:
    orders: list[IntendedOrder] = []

    # Only consider symbols the strategy explicitly targets. Untracked broker holdings
    # are deliberately ignored (defensive: never liquidate a user's manual position).
    for symbol, target_weight in targets.items():
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        asset_class = asset_classes.get(symbol, "equity")
        current_qty = bot_positions.get(symbol, 0.0)

        target_qty = size_position(
            target_weight=target_weight,
            equity_for_sizing=equity_for_sizing,
            price=price,
            max_position_pct=max_position_pct,
            asset_class=asset_class,
        )

        delta = target_qty - current_qty
        if abs(delta) < _DUST_QTY_EPS:
            continue

        side: str = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        cid = _build_client_order_id(strategy_name, symbol, bar_ts_ms, side)

        orders.append(
            IntendedOrder(
                strategy=strategy_name,
                symbol=symbol,
                side=side,    # type: ignore[arg-type]
                qty=qty,
                price=price,
                asset_class=asset_class,
                bar_ts_ms=bar_ts_ms,
                client_order_id=cid,
            )
        )

    return orders
