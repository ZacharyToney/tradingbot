"""Order runner: connects reconciler output → gates → broker (or dry-run log)
and persists everything to SQLite. Uses a fake broker injected via constructor.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tradingbot.config import Settings
from tradingbot.db import connect
from tradingbot.execution.broker import OrderResult, Quote
from tradingbot.execution.runner import OrderRunner
from tradingbot.risk.gates import AccountState, IntendedOrder


def _settings(**kw):
    base = dict(
        alpaca_api_key="k",
        alpaca_secret="s",
        max_position_pct=0.05,
        max_concurrent_positions=3,
        daily_loss_limit_pct=0.02,
        total_dd_limit_pct=0.10,
        clock_skew_max_seconds=5,
        equity_drift_max_pct=0.01,
        equity_data_feed="iex",
    )
    base.update(kw)
    return Settings(**base)


def _state(repo_root: Path, **overrides) -> AccountState:
    base = dict(
        equity_now=100_000.0,
        equity_start_of_day=100_000.0,
        equity_all_time_high=100_000.0,
        last_recon_equity=100_000.0,
        broker_equity=100_000.0,
        clock_skew_seconds=1.0,
        current_open_positions=0,
        current_qty_for_symbol=0.0,
        daytrades_in_5d=0,
        now=datetime(2025, 5, 13, 15, 0, tzinfo=UTC),
        repo_root=repo_root,
    )
    base.update(overrides)
    return AccountState(**base)


def _order() -> IntendedOrder:
    return IntendedOrder(
        strategy="rsi2",
        symbol="SPY",
        side="buy",
        qty=10.0,
        price=100.0,
        asset_class="equity",
        bar_ts_ms=1_700_000_000_000,
        client_order_id="cid-fixed-1",
    )


@dataclass
class FakeBroker:
    calls: list[tuple] = None
    next_result: OrderResult = None
    next_quote: Quote | None = None

    def __post_init__(self):
        self.calls = []

    def submit_market_order(
        self, symbol: str, side: str, qty: float, client_order_id: str, time_in_force: str = "day"
    ) -> OrderResult:
        self.calls.append((symbol, side, qty, client_order_id))
        return self.next_result or OrderResult(
            client_order_id=client_order_id,
            broker_order_id="bro-1",
            status="accepted",
            filled_qty=0.0,
            filled_avg_price=None,
        )

    def get_latest_quote(self, symbol: str) -> Quote | None:
        return self.next_quote


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "tb.db")


def test_dry_run_persists_order_status_dry_run_and_does_not_call_broker(tmp_path):
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=True)

    decision = runner.process_one(_order(), _state(tmp_path))
    assert decision.allow

    assert broker.calls == []
    rows = runner.db.execute("SELECT client_order_id, status FROM orders").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "dry_run"
    assert rows[0]["client_order_id"] == "cid-fixed-1"


def test_execute_mode_calls_broker_once_and_persists(tmp_path):
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    decision = runner.process_one(_order(), _state(tmp_path))
    assert decision.allow

    assert len(broker.calls) == 1
    symbol, side, qty, cid = broker.calls[0]
    assert (symbol, side, qty, cid) == ("SPY", "buy", 10.0, "cid-fixed-1")

    rows = runner.db.execute("SELECT client_order_id, status FROM orders").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"


def test_gate_reject_logs_to_gate_log_and_does_not_submit(tmp_path):
    (tmp_path / "HALT").touch()  # forces halt-file rejection
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    decision = runner.process_one(_order(), _state(tmp_path))
    assert not decision.allow

    assert broker.calls == []
    rows = runner.db.execute("SELECT decision, reason FROM gate_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == "reject"
    assert "halt" in rows[0]["reason"].lower()


def test_duplicate_client_order_id_does_not_double_persist(tmp_path):
    """Submitting the same intended order twice in execute mode results in one DB row."""
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    runner.process_one(_order(), _state(tmp_path))
    runner.process_one(_order(), _state(tmp_path))

    rows = runner.db.execute("SELECT client_order_id FROM orders").fetchall()
    assert len(rows) == 1


def _sell_order(symbol: str = "SOL/USD", qty: float = 56.058) -> IntendedOrder:
    return IntendedOrder(
        strategy="donchian",
        symbol=symbol,
        side="sell",
        qty=qty,
        price=85.0,
        asset_class="crypto",
        bar_ts_ms=1_700_000_000_000,
        client_order_id="cid-sell-1",
    )


def test_sell_clamp_reduces_qty_to_broker_balance(tmp_path):
    """If reconciler asks to sell more SOL than the broker actually holds, the runner
    clamps the submit qty down to what's available. Prevents Alpaca rejecting the
    sell with 'insufficient balance' on in-kind-fee drift."""
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    # Bot thinks it has 56.058 SOL (intended sell qty); broker says 55.918.
    decision = runner.process_one(
        _sell_order(qty=56.058),
        _state(tmp_path),
        broker_positions_canon={"SOLUSD": 55.918},
    )
    assert decision.allow
    assert len(broker.calls) == 1
    symbol, side, qty, _cid = broker.calls[0]
    assert (symbol, side) == ("SOL/USD", "sell")
    assert qty == 55.918  # clamped, not the original 56.058


def test_sell_skipped_when_broker_has_no_position(tmp_path):
    """Defensive: if the broker says we hold zero of the symbol, don't submit a sell
    that will obviously reject. Persist a rejected row for the audit log instead."""
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    decision = runner.process_one(
        _sell_order(),
        _state(tmp_path),
        broker_positions_canon={},  # no SOL position
    )
    assert not decision.allow
    assert broker.calls == []
    row = runner.db.execute(
        "SELECT status, reject_reason FROM orders WHERE client_order_id='cid-sell-1'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert "no broker position" in row["reject_reason"]


def test_quote_captured_at_submit_persists_to_orders_row(tmp_path):
    settings = _settings()
    broker = FakeBroker()
    broker.next_quote = Quote(bid=99.95, ask=100.05, bid_size=10, ask_size=10, ts_ms=1234567)
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    runner.process_one(_order(), _state(tmp_path))

    row = runner.db.execute(
        "SELECT quote_bid, quote_ask, quote_ts_ms, intended_price FROM orders "
        "WHERE client_order_id='cid-fixed-1'"
    ).fetchone()
    assert row["quote_bid"] == 99.95
    assert row["quote_ask"] == 100.05
    assert row["quote_ts_ms"] == 1234567
    assert row["intended_price"] == 100.0  # from IntendedOrder.price


def test_quote_unavailable_does_not_block_submit(tmp_path):
    """If the quote endpoint fails, store NULL for the quote fields but still submit
    the order. Measurement is secondary; trades go through."""
    settings = _settings()
    broker = FakeBroker()
    broker.next_quote = None  # quote unavailable
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    decision = runner.process_one(_order(), _state(tmp_path))
    assert decision.allow
    assert len(broker.calls) == 1  # order still submitted

    row = runner.db.execute(
        "SELECT quote_bid, quote_ask, intended_price FROM orders "
        "WHERE client_order_id='cid-fixed-1'"
    ).fetchone()
    assert row["quote_bid"] is None
    assert row["quote_ask"] is None
    assert row["intended_price"] == 100.0  # still captured from bar close


def test_dry_run_also_captures_quote(tmp_path):
    """Dry-run path needs quote capture too so slippage research includes counterfactuals."""
    settings = _settings()
    broker = FakeBroker()
    broker.next_quote = Quote(bid=99.5, ask=100.5, bid_size=5, ask_size=5, ts_ms=999)
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=True)

    runner.process_one(_order(), _state(tmp_path))

    row = runner.db.execute(
        "SELECT status, quote_bid, quote_ask FROM orders WHERE client_order_id='cid-fixed-1'"
    ).fetchone()
    assert row["status"] == "dry_run"
    assert row["quote_bid"] == 99.5
    assert row["quote_ask"] == 100.5


def test_opposite_side_in_flight_blocks_submit(tmp_path):
    """Regression for the 2026-05-20 wash-trade error: if a BUY is partially filled
    and still open at the broker, the runner must refuse to submit a SELL on the
    same symbol — Alpaca rejects it as a wash trade, and the cleaner thing is to
    short-circuit at our layer with a clear audit-log row."""
    settings = _settings()
    broker = FakeBroker()
    db = _conn(tmp_path)
    # Pre-existing buy still open at broker.
    db.execute(
        """INSERT INTO orders
           (client_order_id, broker_order_id, strategy, symbol, side, qty,
            order_type, limit_price, status, submitted_at_ms, updated_at_ms, reject_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cid-buy-open", "bro-x", "rsi2", "GOOGL", "buy", 12.0,
         "market", None, "partially_filled", 0, 0, None),
    )
    runner = OrderRunner(settings=settings, broker=broker, db=db, dry_run=False)

    sell_order = IntendedOrder(
        strategy="rsi2", symbol="GOOGL", side="sell", qty=10.0, price=388.0,
        asset_class="equity", bar_ts_ms=1_700_000_000_000,
        client_order_id="cid-sell-attempt",
    )
    # State has a real long position in GOOGL (broker truth), so the long-only-equities
    # gate passes — that's what made the prod bug land at the broker instead of being
    # caught by the gates.
    state = _state(tmp_path, current_qty_for_symbol=10.0)
    decision = runner.process_one(sell_order, state)
    assert not decision.allow
    assert "opposite side in flight" in decision.reason
    assert broker.calls == []  # no submit to Alpaca
    row = db.execute(
        "SELECT status, reject_reason FROM orders WHERE client_order_id='cid-sell-attempt'"
    ).fetchone()
    assert row["status"] == "rejected"
    assert "opposite side in flight" in row["reject_reason"]


def test_same_side_in_flight_does_not_block(tmp_path):
    """Same-side duplicates are handled by the cid-uniqueness check, not this guard."""
    settings = _settings()
    broker = FakeBroker()
    db = _conn(tmp_path)
    db.execute(
        """INSERT INTO orders
           (client_order_id, broker_order_id, strategy, symbol, side, qty,
            order_type, limit_price, status, submitted_at_ms, updated_at_ms, reject_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cid-buy-1", "bro-y", "rsi2", "SPY", "buy", 10.0,
         "market", None, "new", 0, 0, None),
    )
    runner = OrderRunner(settings=settings, broker=broker, db=db, dry_run=False)

    # A NEW buy for SPY (different cid) — this isn't the opposite-side case.
    new_buy = IntendedOrder(
        strategy="rsi2", symbol="SPY", side="buy", qty=5.0, price=100.0,
        asset_class="equity", bar_ts_ms=1_700_000_000_000,
        client_order_id="cid-buy-2",
    )
    decision = runner.process_one(new_buy, _state(tmp_path))
    assert decision.allow
    assert len(broker.calls) == 1


def test_buy_not_affected_by_broker_positions(tmp_path):
    """The sell-side clamp must not touch buy orders. Buys can size up freely; only
    sells need protection from drift between recorded and actual balances."""
    settings = _settings()
    broker = FakeBroker()
    runner = OrderRunner(settings=settings, broker=broker, db=_conn(tmp_path), dry_run=False)

    runner.process_one(
        _order(),  # SPY buy, qty=10
        _state(tmp_path),
        broker_positions_canon={"SPY": 0.0},  # no existing position
    )
    assert len(broker.calls) == 1
    assert broker.calls[0][2] == 10.0  # qty unchanged
