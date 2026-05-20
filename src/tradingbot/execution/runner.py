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
from tradingbot.execution.broker import OrderResult, Quote
from tradingbot.risk.gates import AccountState, IntendedOrder, pre_trade
from tradingbot.risk.limits import Decision

# Mirror of live.loop._OPEN_ORDER_STATUSES. Duplicated to avoid an import cycle
# (loop imports runner). Both must stay in sync.
_OPEN_ORDER_STATUSES_FOR_RUNNER = ("new", "accepted", "pending_new", "partially_filled")


class _BrokerLike(Protocol):
    def submit_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> OrderResult: ...

    def get_latest_quote(self, symbol: str) -> Quote | None: ...


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

    def process_one(
        self,
        order: IntendedOrder,
        state: AccountState,
        broker_positions_canon: dict[str, float] | None = None,
    ) -> Decision:
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

        # Opposite-side-in-flight check: refuse to submit a sell when a buy on the
        # same symbol is still open at the broker (and vice versa). Alpaca rejects
        # these as wash trades anyway; we catch it earlier so the failure is one
        # clean rejected row in the audit log instead of a broker HTTPError stack
        # trace. Seen in production on 2026-05-20 when GOOGL partial-fill math
        # over-counted in-flight qty and emitted a wrong-side order.
        opposite_side = "sell" if order.side == "buy" else "buy"
        open_opposite = self.db.execute(
            f"""SELECT client_order_id FROM orders
                WHERE symbol = ? AND side = ?
                  AND status IN ({",".join("?" * len(_OPEN_ORDER_STATUSES_FOR_RUNNER))})
                LIMIT 1""",
            (order.symbol, opposite_side, *_OPEN_ORDER_STATUSES_FOR_RUNNER),
        ).fetchone()
        if open_opposite is not None:
            logger.warning(
                f"skip {order.side}: opposite-side order in flight for {order.symbol} "
                f"(blocking cid={open_opposite['client_order_id']}, "
                f"new cid={order.client_order_id})"
            )
            self._persist_order(
                order,
                status="rejected",
                broker_order_id=None,
                reject_reason="opposite side in flight",
                quote=None,
            )
            return Decision(False, "opposite side in flight")

        # Sell-side clamp: never ask the broker to sell more than it actually holds.
        # Protects against drift between recorded fills and real balance (in-kind fees,
        # external transfers, etc.). The reconciler computes qty from bot_positions,
        # which is already broker-truth-backed, but defending here too is cheap.
        submit_qty = order.qty
        if order.side == "sell" and broker_positions_canon is not None:
            from tradingbot.execution.broker import canon_symbol
            broker_qty = broker_positions_canon.get(canon_symbol(order.symbol), 0.0)
            if broker_qty <= 0:
                logger.warning(
                    f"sell skipped: broker has no long position for {order.symbol} "
                    f"(intended qty={order.qty}). cid={order.client_order_id}"
                )
                self._persist_order(
                    order,
                    status="rejected",
                    broker_order_id=None,
                    reject_reason="no broker position to sell",
                    quote=None,
                )
                return Decision(False, "no broker position to sell")
            if broker_qty < submit_qty:
                logger.info(
                    f"sell qty clamp: intended={order.qty} broker_qty={broker_qty} "
                    f"cid={order.client_order_id}"
                )
                submit_qty = broker_qty

        # Quote snapshot for slippage measurement. Best-effort — never block a trade.
        quote: Quote | None = None
        try:
            quote = self.broker.get_latest_quote(order.symbol)
        except Exception as e:  # belt-and-suspenders; broker already swallows
            logger.warning(f"get_latest_quote raised unexpectedly: {e}")

        if self.dry_run:
            logger.info(
                f"dry_run order strategy={order.strategy} symbol={order.symbol} "
                f"side={order.side} qty={submit_qty} cid={order.client_order_id}"
            )
            self._persist_order(
                order, status="dry_run", broker_order_id=None, reject_reason=None, quote=quote,
            )
            return decision

        # Live submit. Alpaca rejects "day" TIF for crypto — use GTC instead.
        tif = "gtc" if order.asset_class == "crypto" else "day"
        try:
            result = self.broker.submit_market_order(
                symbol=order.symbol,
                side=order.side,
                qty=submit_qty,
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
                quote=quote,
            )
            return Decision(False, f"broker error: {e}")

        self._persist_order(
            order,
            status=result.status,
            broker_order_id=result.broker_order_id,
            reject_reason=None,
            quote=quote,
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
        quote: Quote | None = None,
    ) -> None:
        now = utc_now_ms()
        self.db.execute(
            """INSERT INTO orders
               (client_order_id, broker_order_id, strategy, symbol, side, qty,
                order_type, limit_price, status, submitted_at_ms, updated_at_ms,
                reject_reason, quote_bid, quote_ask, quote_ts_ms, intended_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                quote.bid if quote else None,
                quote.ask if quote else None,
                quote.ts_ms if quote else None,
                order.price,
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
