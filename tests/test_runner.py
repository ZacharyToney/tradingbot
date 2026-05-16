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
from tradingbot.execution.broker import OrderResult
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
