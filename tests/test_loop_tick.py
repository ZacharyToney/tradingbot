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
