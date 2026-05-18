"""End-to-end happy path for a single live-loop tick. No real network — all dependencies
are fakes injected via constructor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from tradingbot.clock import ClockCheck
from tradingbot.config import Settings
from tradingbot.db import connect
from tradingbot.execution.broker import (
    AccountSnapshot,
    OrderResult,
    PositionSnapshot,
)
from tradingbot.live.loop import LiveContext, tick


def _zero_skew():
    return ClockCheck(ok=True, skew_seconds=0.0)


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


@dataclass
class FakeBroker:
    equity: float = 100_000.0
    positions: list[PositionSnapshot] = field(default_factory=list)
    submitted: list[tuple] = field(default_factory=list)
    # cid -> OrderResult, for use by `get_order_by_client_id` in sync tests
    order_state: dict[str, OrderResult] = field(default_factory=dict)

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity=self.equity,
            cash=self.equity,
            buying_power=self.equity * 2,
            daytrade_count=0,
        )

    def get_positions(self) -> list[PositionSnapshot]:
        return list(self.positions)

    def submit_market_order(
        self, symbol: str, side: str, qty: float, client_order_id: str, time_in_force: str = "day"
    ) -> OrderResult:
        self.submitted.append((symbol, side, qty, client_order_id))
        return OrderResult(
            client_order_id=client_order_id,
            broker_order_id=f"bro-{len(self.submitted)}",
            status="accepted",
            filled_qty=0.0,
            filled_avg_price=None,
        )

    def get_order_by_client_id(self, cid: str) -> OrderResult | None:
        return self.order_state.get(cid)


@dataclass
class FakeBarSource:
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)

    def get_bars(self, symbol, timeframe, start, end):
        return self.bars.get(symbol, pd.DataFrame())


def _flat_bars(symbol: str = "SPY") -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=10, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * 10,
            "high": [100.5] * 10,
            "low": [99.5] * 10,
            "close": [100.0] * 10,
            "volume": [1_000_000] * 10,
        },
        index=idx,
    )


@dataclass
class FakeStrategy:
    """Deterministic strategy for wiring tests. Always emits +1.0 for SPY."""
    name: str = "fake"
    universe: list[str] = field(default_factory=lambda: ["SPY"])
    target_weight: float = 1.0

    @property
    def timeframe(self):
        from tradingbot.data.bars import TF
        return TF(1, "Day")

    def generate_signals(self, symbol: str, df):
        return pd.Series(self.target_weight, index=df.index)


def test_tick_dry_run_persists_signal_and_dry_run_order(tmp_path: Path):
    settings = _settings()
    broker = FakeBroker()
    bars = FakeBarSource(bars={"SPY": _flat_bars()})
    db = connect(tmp_path / "tb.db")
    strategy = FakeStrategy()

    ctx = LiveContext(
        settings=settings,
        db=db,
        broker=broker,
        bar_source=bars,
        strategies=[strategy],
        repo_root=tmp_path,
        dry_run=True,
        now=datetime(2025, 5, 13, 15, 0, tzinfo=UTC),
        universe_override=["SPY"],
        skew_probe=_zero_skew,
    )

    tick(ctx)

    sigs = db.execute("SELECT strategy, symbol, target_weight FROM signals").fetchall()
    assert len(sigs) == 1
    assert sigs[0]["symbol"] == "SPY"
    assert sigs[0]["target_weight"] == 1.0

    orders = db.execute("SELECT side, status FROM orders").fetchall()
    assert len(orders) == 1
    assert orders[0]["status"] == "dry_run"
    assert orders[0]["side"] == "buy"
    assert broker.submitted == []


def test_tick_execute_calls_broker(tmp_path: Path):
    settings = _settings()
    broker = FakeBroker()
    bars = FakeBarSource(bars={"SPY": _flat_bars()})
    db = connect(tmp_path / "tb.db")
    strategy = FakeStrategy()

    ctx = LiveContext(
        settings=settings,
        db=db,
        broker=broker,
        bar_source=bars,
        strategies=[strategy],
        repo_root=tmp_path,
        dry_run=False,
        now=datetime(2025, 5, 13, 15, 0, tzinfo=UTC),
        universe_override=["SPY"],
        skew_probe=_zero_skew,
    )

    tick(ctx)

    assert len(broker.submitted) == 1
    sym, side, qty, cid = broker.submitted[0]
    assert (sym, side) == ("SPY", "buy")
    assert qty > 0
    orders = db.execute("SELECT status FROM orders").fetchall()
    assert orders[0]["status"] == "accepted"


def test_tick_halt_file_blocks_orders(tmp_path: Path):
    (tmp_path / "HALT").touch()
    settings = _settings()
    broker = FakeBroker()
    bars = FakeBarSource(bars={"SPY": _flat_bars()})
    db = connect(tmp_path / "tb.db")
    strategy = FakeStrategy()

    ctx = LiveContext(
        settings=settings,
        db=db,
        broker=broker,
        bar_source=bars,
        strategies=[strategy],
        repo_root=tmp_path,
        dry_run=False,
        now=datetime(2025, 5, 13, 15, 0, tzinfo=UTC),
        universe_override=["SPY"],
        skew_probe=_zero_skew,
    )

    tick(ctx)

    rejects = db.execute("SELECT decision, reason FROM gate_log WHERE decision='reject'").fetchall()
    assert len(rejects) >= 1
    assert any("halt" in r["reason"].lower() for r in rejects)
    assert db.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 0
    assert broker.submitted == []


# ---- _sync_open_orders ----------------------------------------------------

def _insert_order(db, cid, symbol, side, qty, status, quote_bid=None, quote_ask=None):
    """Helper: stuff a row into orders for the sync tests."""
    db.execute(
        """INSERT INTO orders
           (client_order_id, broker_order_id, strategy, symbol, side, qty,
            order_type, limit_price, status, submitted_at_ms, updated_at_ms, reject_reason,
            quote_bid, quote_ask)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, "bro-1", "donchian", symbol, side, qty,
         "market", None, status, 0, 0, None, quote_bid, quote_ask),
    )


def test_sync_open_orders_noop_when_no_open_orders(tmp_path):
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-A", "SOL/USD", "buy", 10.0, status="filled")  # already terminal
    broker = FakeBroker()
    n = _sync_open_orders(db, broker)
    assert n == 0
    # Status untouched
    row = db.execute("SELECT status FROM orders WHERE client_order_id='cid-A'").fetchone()
    assert row["status"] == "filled"


def test_sync_open_orders_updates_status_and_inserts_fill(tmp_path):
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-B", "SOL/USD", "buy", 10.0, status="pending_new")
    broker = FakeBroker(order_state={
        "cid-B": OrderResult(
            client_order_id="cid-B", broker_order_id="bro-B",
            status="filled", filled_qty=10.0, filled_avg_price=88.5,
        ),
    })
    n = _sync_open_orders(db, broker)
    assert n == 1
    row = db.execute("SELECT status FROM orders WHERE client_order_id='cid-B'").fetchone()
    assert row["status"] == "filled"
    fill = db.execute("SELECT qty, price FROM fills WHERE client_order_id='cid-B'").fetchone()
    assert fill is not None
    assert fill["qty"] == 10.0
    assert fill["price"] == 88.5


def test_sync_open_orders_no_change_when_status_unchanged(tmp_path):
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-C", "SOL/USD", "buy", 10.0, status="pending_new")
    broker = FakeBroker(order_state={
        "cid-C": OrderResult(
            client_order_id="cid-C", broker_order_id="bro-C",
            status="pending_new", filled_qty=0.0, filled_avg_price=None,
        ),
    })
    n = _sync_open_orders(db, broker)
    assert n == 0  # no state change
    fills = db.execute("SELECT COUNT(*) AS c FROM fills").fetchone()
    assert fills["c"] == 0


def test_sync_open_orders_skips_when_broker_returns_none(tmp_path):
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-D", "SOL/USD", "buy", 10.0, status="pending_new")
    broker = FakeBroker()  # order_state empty → returns None
    n = _sync_open_orders(db, broker)
    assert n == 0
    row = db.execute("SELECT status FROM orders WHERE client_order_id='cid-D'").fetchone()
    assert row["status"] == "pending_new"  # untouched


# ---- slippage + fee computation in _sync_open_orders ---------------------

def test_sync_fill_computes_slippage_for_buy_against_ask(tmp_path):
    """Buy filled above ask → positive slippage (paid worse than market)."""
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-buy-1", "SPY", "buy", 10.0, status="pending_new",
                  quote_bid=99.95, quote_ask=100.00)
    broker = FakeBroker(order_state={
        "cid-buy-1": OrderResult(
            client_order_id="cid-buy-1", broker_order_id="bro-buy-1",
            status="filled", filled_qty=10.0, filled_avg_price=100.05,
        ),
    })
    _sync_open_orders(db, broker)
    row = db.execute(
        "SELECT realized_slippage_bps, fee_qty FROM orders WHERE client_order_id='cid-buy-1'"
    ).fetchone()
    # (100.05 - 100.00) / 100.00 * 10000 = 5.0 bps, positive sign for buy
    assert abs(row["realized_slippage_bps"] - 5.0) < 1e-6
    assert row["fee_qty"] == 0.0  # equity, no fee


def test_sync_fill_computes_slippage_for_sell_against_bid(tmp_path):
    """Sell filled below bid → positive slippage (received less than market)."""
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-sell-1", "SPY", "sell", 10.0, status="pending_new",
                  quote_bid=100.00, quote_ask=100.05)
    broker = FakeBroker(order_state={
        "cid-sell-1": OrderResult(
            client_order_id="cid-sell-1", broker_order_id="bro-sell-1",
            status="filled", filled_qty=10.0, filled_avg_price=99.95,
        ),
    })
    _sync_open_orders(db, broker)
    row = db.execute(
        "SELECT realized_slippage_bps FROM orders WHERE client_order_id='cid-sell-1'"
    ).fetchone()
    # sign = -1 for sell; (99.95 - 100.00) / 100.00 * 10000 * -1 = 5.0 bps positive
    assert abs(row["realized_slippage_bps"] - 5.0) < 1e-6


def test_sync_fill_handles_null_quote_gracefully(tmp_path):
    """If no quote was captured, slippage stays NULL but the fill still records."""
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-no-q", "SPY", "buy", 10.0, status="pending_new",
                  quote_bid=None, quote_ask=None)
    broker = FakeBroker(order_state={
        "cid-no-q": OrderResult(
            client_order_id="cid-no-q", broker_order_id="bro-no-q",
            status="filled", filled_qty=10.0, filled_avg_price=100.0,
        ),
    })
    _sync_open_orders(db, broker)
    row = db.execute(
        "SELECT status, realized_slippage_bps FROM orders WHERE client_order_id='cid-no-q'"
    ).fetchone()
    assert row["status"] == "filled"
    assert row["realized_slippage_bps"] is None


def test_sync_fill_detects_in_kind_fee_for_crypto_buy(tmp_path):
    """Crypto buy: Alpaca says it filled 56.058 SOL but broker only shows 55.918.
    The 0.14 gap is the in-kind fee."""
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-sol", "SOL/USD", "buy", 56.058, status="pending_new",
                  quote_bid=88.0, quote_ask=88.5)
    broker = FakeBroker(order_state={
        "cid-sol": OrderResult(
            client_order_id="cid-sol", broker_order_id="bro-sol",
            status="filled", filled_qty=56.058, filled_avg_price=88.33,
        ),
    })
    _sync_open_orders(db, broker, broker_positions_canon={"SOLUSD": 55.918})
    row = db.execute(
        "SELECT fee_qty, realized_slippage_bps FROM orders WHERE client_order_id='cid-sol'"
    ).fetchone()
    assert abs(row["fee_qty"] - 0.14) < 1e-6
    # buy: (88.33 - 88.5) / 88.5 * 10000 = negative (price improvement on this synthetic test)
    assert row["realized_slippage_bps"] is not None


def test_sync_fill_no_fee_on_equity(tmp_path):
    from tradingbot.live.loop import _sync_open_orders

    db = connect(tmp_path / "tb.db")
    _insert_order(db, "cid-amzn", "AMZN", "buy", 18.0, status="pending_new",
                  quote_bid=264.0, quote_ask=264.2)
    broker = FakeBroker(order_state={
        "cid-amzn": OrderResult(
            client_order_id="cid-amzn", broker_order_id="bro-amzn",
            status="filled", filled_qty=18.0, filled_avg_price=264.11,
        ),
    })
    _sync_open_orders(db, broker, broker_positions_canon={"AMZN": 18.0})
    row = db.execute(
        "SELECT fee_qty FROM orders WHERE client_order_id='cid-amzn'"
    ).fetchone()
    assert row["fee_qty"] == 0.0


# ---- _bot_positions ------------------------------------------------------

def test_bot_positions_uses_broker_truth_for_filled_qty(tmp_path):
    """Reads filled qty from the broker snapshot, NOT the fills table — this is the
    fix for in-kind crypto fees where settled balance < submitted qty."""
    from tradingbot.live.loop import _bot_positions

    db = connect(tmp_path / "tb.db")
    # Audit-log fill would say 56.058 (the qty the bot submitted), but broker shows 55.918.
    # The function must return broker truth.
    bot_positions = _bot_positions(
        db,
        strategy_name="donchian",
        symbols=["SOL/USD", "BTC/USD"],
        broker_positions_canon={"SOLUSD": 55.918, "BTCUSD": 0.0},
    )
    assert bot_positions == {"SOL/USD": 55.918, "BTC/USD": 0.0}


def test_bot_positions_zero_when_broker_has_no_record(tmp_path):
    from tradingbot.live.loop import _bot_positions

    db = connect(tmp_path / "tb.db")
    bot_positions = _bot_positions(
        db,
        strategy_name="rsi2",
        symbols=["SPY", "QQQ"],
        broker_positions_canon={},  # broker has nothing
    )
    assert bot_positions == {"SPY": 0.0, "QQQ": 0.0}


def test_bot_positions_layers_in_flight_orders_on_top_of_broker_truth(tmp_path):
    """Open (unfilled) orders count toward the effective position so the reconciler
    doesn't re-issue the same buy/sell while a previous order is still pending."""
    from tradingbot.live.loop import _bot_positions

    db = connect(tmp_path / "tb.db")
    # An in-flight buy of 18 AMZN at the broker, status=new.
    _insert_order(db, "cid-amzn-1", "AMZN", "buy", 18.0, status="new")
    # Broker shows no AMZN position yet (order hasn't filled).
    bot_positions = _bot_positions(
        db,
        strategy_name="donchian",  # query is keyed by strategy
        symbols=["AMZN"],
        broker_positions_canon={"AMZN": 0.0},
    )
    # 0 broker + 18 in-flight = 18 effective.
    assert bot_positions == {"AMZN": 18.0}


def test_bot_positions_symbol_canonicalization_for_crypto(tmp_path):
    """The broker returns 'SOLUSD' but the strategy/universe key is 'SOL/USD'.
    canon_symbol() must bridge them."""
    from tradingbot.live.loop import _bot_positions

    db = connect(tmp_path / "tb.db")
    bot_positions = _bot_positions(
        db,
        strategy_name="donchian",
        symbols=["SOL/USD"],
        broker_positions_canon={"SOLUSD": 42.5},  # Alpaca's form
    )
    assert bot_positions == {"SOL/USD": 42.5}
