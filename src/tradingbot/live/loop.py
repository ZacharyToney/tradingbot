"""Live trading loop. Single-threaded foreground process.

Each tick:
  1. Heartbeat + (periodic) equity snapshot.
  2. For each strategy: pull bars, compute signals for the last completed bar,
     persist signals.
  3. If signals haven't been processed AND market is open for this asset class:
     reconcile → gate → submit (or dry-run log) → persist → mark processed.

Broker is the source of truth for `equity` and `daytrade_count` snapshots. SQLite is
the audit log: orders/fills/signals/gate_log/equity_snapshots.
"""
from __future__ import annotations

import contextlib
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
from loguru import logger

from tradingbot.clock import (
    AssetClass,
    ClockCheck,
    check_ntp_skew,
    is_us_equity_market_open,
    last_completed_daily_bar_ts,
    utc_now_ms,
)
from tradingbot.config import Settings
from tradingbot.data.bars import TF
from tradingbot.execution.reconciler import reconcile
from tradingbot.execution.runner import OrderRunner
from tradingbot.risk.gates import AccountState

_DEFAULT_UNIVERSE_EQUITY = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]


class _StrategyLike(Protocol):
    name: str
    universe: list[str]
    timeframe: TF

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series: ...


class _BrokerLike(Protocol):
    def get_account(self): ...
    def get_positions(self): ...
    def submit_market_order(self, symbol, side, qty, client_order_id, time_in_force="day"): ...


class _BarSourceLike(Protocol):
    def get_bars(self, symbol, timeframe, start, end): ...


def _default_skew_probe() -> ClockCheck:
    return check_ntp_skew()


def _zero_skew_probe() -> ClockCheck:
    return ClockCheck(ok=True, skew_seconds=0.0)


@dataclass
class LiveContext:
    settings: Settings
    db: sqlite3.Connection
    broker: _BrokerLike
    bar_source: _BarSourceLike
    strategies: list[_StrategyLike]
    repo_root: Path
    dry_run: bool = True
    now: datetime | None = None
    universe_override: list[str] | None = None
    halt_requested: bool = False
    skew_probe: callable | None = None  # tests inject _zero_skew_probe

    def get_now(self) -> datetime:
        return self.now or datetime.now(UTC)

    def get_skew(self) -> ClockCheck:
        return (self.skew_probe or _default_skew_probe)()


def _classify(symbol: str) -> AssetClass:
    return "crypto" if "/" in symbol else "equity"


def _equity_for_sizing(db: sqlite3.Connection, broker: _BrokerLike) -> float:
    row = db.execute("SELECT equity FROM starting_equity WHERE id = 1").fetchone()
    if row is not None:
        return float(row["equity"])
    acc = broker.get_account()
    db.execute(
        "INSERT OR REPLACE INTO starting_equity (id, equity, captured_at_ms) VALUES (1, ?, ?)",
        (acc.equity, utc_now_ms()),
    )
    logger.info(f"starting_equity captured equity={acc.equity:.2f}")
    return float(acc.equity)


def _start_of_day_equity(db: sqlite3.Connection, current_equity: float) -> float:
    """Equity at this trading day's first snapshot. If none today, snapshot now."""
    today_utc = datetime.now(UTC).date()
    start_ms = int(
        datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=UTC).timestamp() * 1000
    )
    row = db.execute(
        "SELECT equity FROM equity_snapshots WHERE ts_ms >= ? ORDER BY ts_ms ASC LIMIT 1",
        (start_ms,),
    ).fetchone()
    if row is not None:
        return float(row["equity"])
    return current_equity


def _equity_all_time_high(db: sqlite3.Connection, current: float) -> float:
    row = db.execute("SELECT MAX(equity) AS m FROM equity_snapshots").fetchone()
    high = float(row["m"]) if row and row["m"] is not None else 0.0
    return max(high, current)


def _last_recon_equity(db: sqlite3.Connection) -> float | None:
    row = db.execute(
        "SELECT equity FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    return float(row["equity"]) if row else None


def _snapshot_equity(db: sqlite3.Connection, broker: _BrokerLike) -> float:
    acc = broker.get_account()
    db.execute(
        "INSERT OR REPLACE INTO equity_snapshots (ts_ms, equity, cash, buying_power) "
        "VALUES (?, ?, ?, ?)",
        (utc_now_ms(), acc.equity, acc.cash, acc.buying_power),
    )
    return float(acc.equity)


def _bot_positions(
    db: sqlite3.Connection, strategy_name: str, symbols: list[str]
) -> dict[str, float]:
    """Sum fills for this strategy by symbol (buy adds, sell subtracts)."""
    positions: dict[str, float] = dict.fromkeys(symbols, 0.0)
    rows = db.execute(
        """SELECT f.symbol, f.side, f.qty
           FROM fills f
           JOIN orders o ON f.client_order_id = o.client_order_id
           WHERE o.strategy = ?""",
        (strategy_name,),
    ).fetchall()
    for r in rows:
        sign = 1.0 if r["side"] == "buy" else -1.0
        positions[r["symbol"]] = positions.get(r["symbol"], 0.0) + sign * float(r["qty"])
    return positions


def _signal_already_recorded(
    db: sqlite3.Connection, strategy: str, symbol: str, bar_ts_ms: int
) -> bool:
    row = db.execute(
        "SELECT 1 FROM signals WHERE strategy=? AND symbol=? AND bar_ts_ms=?",
        (strategy, symbol, bar_ts_ms),
    ).fetchone()
    return row is not None


def _record_signal(
    db: sqlite3.Connection,
    strategy: str,
    symbol: str,
    bar_ts_ms: int,
    target_weight: float,
    note: str = "",
) -> None:
    db.execute(
        """INSERT OR IGNORE INTO signals
           (strategy, symbol, bar_ts_ms, target_weight, note, created_at_ms)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (strategy, symbol, bar_ts_ms, target_weight, note or None, utc_now_ms()),
    )


def _mark_signal_processed(
    db: sqlite3.Connection, strategy: str, symbol: str, bar_ts_ms: int
) -> None:
    db.execute(
        "UPDATE signals SET processed_at_ms = ? "
        "WHERE strategy = ? AND symbol = ? AND bar_ts_ms = ?",
        (utc_now_ms(), strategy, symbol, bar_ts_ms),
    )


def tick(ctx: LiveContext) -> None:
    """One iteration. Safe to call repeatedly; idempotent against same bar."""
    now = ctx.get_now()

    skew = ctx.get_skew()
    current_equity = _snapshot_equity(ctx.db, ctx.broker)
    starting_equity = _equity_for_sizing(ctx.db, ctx.broker)

    for strategy in ctx.strategies:
        universe = ctx.universe_override or strategy.universe
        # For v1 we treat all strategies as daily.
        ac = _classify(universe[0]) if universe else "equity"
        bar_ts = last_completed_daily_bar_ts(now, ac)
        bar_ts_ms = int(bar_ts.timestamp() * 1000)

        prices: dict[str, float] = {}
        targets: dict[str, float] = {}
        asset_classes: dict[str, AssetClass] = {}

        for symbol in universe:
            asset_classes[symbol] = _classify(symbol)
            # Pull enough history for the strategy's longest indicator (SMA(200) + headroom).
            start = bar_ts - timedelta(days=400)
            df = ctx.bar_source.get_bars(
                symbol, strategy.timeframe, start=start, end=bar_ts + timedelta(days=1)
            )
            if df is None or df.empty:
                logger.warning(f"no bars for {symbol}; skipping")
                continue
            # Strategy is a pure function of df[:t].
            sigs = strategy.generate_signals(symbol, df)
            if len(sigs) == 0:
                continue
            w = float(sigs.iloc[-1])
            if pd.isna(w):
                w = 0.0
            targets[symbol] = w
            prices[symbol] = float(df["close"].iloc[-1])
            if not _signal_already_recorded(ctx.db, strategy.name, symbol, bar_ts_ms):
                _record_signal(ctx.db, strategy.name, symbol, bar_ts_ms, w)
                logger.info(
                    f"signal strategy={strategy.name} symbol={symbol} bar={bar_ts.date()} "
                    f"target_weight={w:.4f}"
                )

        if not targets:
            continue

        # Only place orders if the asset class's market is open right now.
        market_open = ac == "crypto" or is_us_equity_market_open(now)
        if not market_open:
            logger.info(f"market_closed asset_class={ac} — signals recorded, no orders this tick")
            continue

        positions = _bot_positions(ctx.db, strategy.name, list(targets))
        orders = reconcile(
            targets=targets,
            bot_positions=positions,
            prices=prices,
            equity_for_sizing=starting_equity,
            max_position_pct=ctx.settings.max_position_pct,
            asset_classes=asset_classes,
            strategy_name=strategy.name,
            bar_ts_ms=bar_ts_ms,
        )

        if not orders:
            for symbol in targets:
                _mark_signal_processed(ctx.db, strategy.name, symbol, bar_ts_ms)
            continue

        runner = OrderRunner(
            settings=ctx.settings, broker=ctx.broker, db=ctx.db, dry_run=ctx.dry_run
        )

        for order in orders:
            current_qty = positions.get(order.symbol, 0.0)
            state = AccountState(
                equity_now=current_equity,
                equity_start_of_day=_start_of_day_equity(ctx.db, current_equity),
                equity_all_time_high=_equity_all_time_high(ctx.db, current_equity),
                last_recon_equity=_last_recon_equity(ctx.db),
                broker_equity=current_equity,
                clock_skew_seconds=skew.skew_seconds,
                current_open_positions=sum(1 for v in positions.values() if v != 0),
                current_qty_for_symbol=current_qty,
                daytrades_in_5d=ctx.broker.get_account().daytrade_count,
                now=now,
                repo_root=ctx.repo_root,
            )
            runner.process_one(order, state)

        for symbol in targets:
            _mark_signal_processed(ctx.db, strategy.name, symbol, bar_ts_ms)


def run_loop(ctx: LiveContext) -> int:
    """Foreground loop. Returns exit code."""
    logger.info(
        f"loop start mode={ctx.settings.trading_mode} dry_run={ctx.dry_run} "
        f"strategies={[s.name for s in ctx.strategies]} "
        f"poll_interval_seconds={ctx.settings.poll_interval_seconds}"
    )

    def _on_signal(signum, _frame):
        logger.warning(f"received signal {signum} — shutting down cleanly")
        ctx.halt_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # ValueError: not main thread (in tests)
            signal.signal(sig, _on_signal)

    while True:
        if (ctx.repo_root / "HALT").exists():
            logger.warning("halt file detected, exiting")
            break
        if ctx.halt_requested:
            logger.warning("halt_requested=true, exiting")
            break
        try:
            tick(ctx)
        except Exception as e:
            logger.exception(f"tick error (loop continues): {e}")
        time.sleep(ctx.settings.poll_interval_seconds)

    logger.info("loop exit")
    return 0
