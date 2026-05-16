"""Apply intended orders: gate → submit (or dry-run) → persist.

Persists to:
  - orders:  one row per accepted/dry_run/rejected attempt (PK = client_order_id)
  - fills:   any fills returned by the broker
  - gate_log: every gate decision (pass + reject), for audit
"""
from __future__ import annotations

import sqlite3
from typing import Protocol

from loguru import logger

from tradingbot.clock import utc_now_ms
from tradingbot.config import Settings
from tradingbot.execution.broker import OrderResult
from tradingbot.risk.gates import AccountState, IntendedOrder, pre_trade
from tradingbot.risk.limits import Decision


class _BrokerLike(Protocol):
    def submit_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> OrderResult: ...


class OrderRunner:
    def __init__(
        self,
        settings: Settings,
        broker: _BrokerLike,
        db: sqlite3.Connection,
        dry_run: bool,
    ):
        self.settings = settings
        self.broker = broker
        self.db = db
        self.dry_run = dry_run

    def process_one(self, order: IntendedOrder, state: AccountState) -> Decision:
        decision = pre_trade(order, state, self.settings)
        self._log_gate(order, decision)

        if not decision.allow:
            logger.info(
                f"gate reject strategy={order.strategy} symbol={order.symbol} "
                f"side={order.side} qty={order.qty} reason={decision.reason}"
            )
            return decision

        # Was this client_order_id already submitted? Skip duplicate work.
        existing = self.db.execute(
            "SELECT status FROM orders WHERE client_order_id = ?",
            (order.client_order_id,),
        ).fetchone()
        if existing is not None:
            logger.info(
                f"duplicate cid={order.client_order_id} status={existing['status']}, skipped"
            )
            return decision

        if self.dry_run:
            logger.info(
                f"dry_run order strategy={order.strategy} symbol={order.symbol} "
                f"side={order.side} qty={order.qty} cid={order.client_order_id}"
            )
            self._persist_order(order, status="dry_run", broker_order_id=None, reject_reason=None)
            return decision

        # Live submit. Alpaca rejects "day" TIF for crypto — use GTC instead.
        tif = "gtc" if order.asset_class == "crypto" else "day"
        try:
            result = self.broker.submit_market_order(
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                client_order_id=order.client_order_id,
                time_in_force=tif,
            )
        except Exception as e:
            logger.exception(f"broker submit failed: {e}")
            self._persist_order(
                order,
                status="rejected",
                broker_order_id=None,
                reject_reason=str(e)[:500],
            )
            return Decision(False, f"broker error: {e}")

        self._persist_order(
            order,
            status=result.status,
            broker_order_id=result.broker_order_id,
            reject_reason=None,
        )
        if result.filled_qty and result.filled_qty > 0 and result.filled_avg_price:
            self._persist_fill(order, result)

        return decision

    def _log_gate(self, order: IntendedOrder, decision: Decision) -> None:
        self.db.execute(
            """INSERT INTO gate_log
               (strategy, symbol, side, qty, decision, reason, bar_ts_ms, created_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.strategy,
                order.symbol,
                order.side,
                order.qty,
                "pass" if decision.allow else "reject",
                decision.reason or None,
                order.bar_ts_ms,
                utc_now_ms(),
            ),
        )

    def _persist_order(
        self,
        order: IntendedOrder,
        status: str,
        broker_order_id: str | None,
        reject_reason: str | None,
    ) -> None:
        now = utc_now_ms()
        self.db.execute(
            """INSERT INTO orders
               (client_order_id, broker_order_id, strategy, symbol, side, qty,
                order_type, limit_price, status, submitted_at_ms, updated_at_ms,
                reject_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.client_order_id,
                broker_order_id,
                order.strategy,
                order.symbol,
                order.side,
                order.qty,
                "market",
                None,
                status,
                now,
                now,
                reject_reason,
            ),
        )

    def _persist_fill(self, order: IntendedOrder, result: OrderResult) -> None:
        # OrderResult doesn't expose fill_id; use broker_order_id as a stable surrogate.
        fill_id = f"{result.broker_order_id}-1"
        self.db.execute(
            """INSERT OR IGNORE INTO fills
               (fill_id, client_order_id, symbol, side, qty, price, filled_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                fill_id,
                order.client_order_id,
                order.symbol,
                order.side,
                result.filled_qty,
                result.filled_avg_price,
                utc_now_ms(),
            ),
        )
