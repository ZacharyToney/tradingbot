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

# Dust thresholds — don't bother submitting trivially small rebalances.
# Equity: 1.0 share (matches the whole-share rounding in `size_position`).
# Crypto: $5 notional. Comfortably above Alpaca's minimum-order floor (~$1) and below
#         what could plausibly matter at any sane equity. Without this asset-class
#         split, small accounts silently drop fractional crypto orders below 1.0 base
#         asset (e.g. 0.089 ETH at $2.8k = $250 was being dropped as "dust").
_DUST_QTY_EPS_EQUITY = 1.0
_DUST_USD_EPS_CRYPTO = 5.0


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
        dust_threshold = (
            _DUST_QTY_EPS_EQUITY
            if asset_class == "equity"
            else _DUST_USD_EPS_CRYPTO / price
        )
        if abs(delta) < dust_threshold:
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
