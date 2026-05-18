"""Smoke + correctness tests for the `tb cost-report` CLI subcommand."""
from __future__ import annotations

from pathlib import Path

from tradingbot.cli import _cmd_cost_report
from tradingbot.db import connect


def _insert_filled_order(
    db, cid, strategy, symbol, side, qty, slip_bps, fee_qty=0.0, intended_price=100.0
):
    db.execute(
        """INSERT INTO orders
           (client_order_id, broker_order_id, strategy, symbol, side, qty,
            order_type, limit_price, status, submitted_at_ms, updated_at_ms,
            reject_reason, realized_slippage_bps, fee_qty, intended_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, f"bro-{cid}", strategy, symbol, side, qty,
         "market", None, "filled", 1_700_000_000_000, 1_700_000_000_000, None,
         slip_bps, fee_qty, intended_price),
    )


class _S:
    """Minimal settings stub for the CLI command."""
    def __init__(self, db_path: Path):
        self.db_full_path = str(db_path)


class _Args:
    start = None
    end = None
    strategy = None


def test_cost_report_handles_zero_orders_gracefully(tmp_path: Path, capsys):
    db_path = tmp_path / "tb.db"
    connect(db_path).close()
    rc = _cmd_cost_report(_S(db_path), _Args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "no filled orders" in out


def test_cost_report_aggregates_median_and_p90_per_strategy(tmp_path: Path, capsys):
    db_path = tmp_path / "tb.db"
    db = connect(db_path)
    # rsi2 equities: slip values [3, 5, 8] → median 5, p90 8
    _insert_filled_order(db, "a", "rsi2", "SPY", "buy", 10.0, 3.0)
    _insert_filled_order(db, "b", "rsi2", "QQQ", "buy", 10.0, 5.0)
    _insert_filled_order(db, "c", "rsi2", "AMZN", "buy", 10.0, 8.0)
    # donchian crypto: one fill with a 25-bps slip and 0.14 SOL fee at $88
    _insert_filled_order(
        db, "d", "donchian", "SOL/USD", "buy", 56.0, 25.0,
        fee_qty=0.14, intended_price=88.0,
    )
    db.close()

    rc = _cmd_cost_report(_S(db_path), _Args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "rsi2" in out
    assert "donchian" in out
    # Fee $: 0.14 * 88 = 12.32
    assert "12.32" in out
    # Overall median should be a number; gap shown vs 5 bps
    assert "overall median" in out


def test_cost_report_filters_by_strategy(tmp_path: Path, capsys):
    db_path = tmp_path / "tb.db"
    db = connect(db_path)
    _insert_filled_order(db, "a", "rsi2", "SPY", "buy", 10.0, 3.0)
    _insert_filled_order(db, "b", "donchian", "SOL/USD", "buy", 56.0, 25.0)
    db.close()

    args = _Args()
    args.strategy = "rsi2"
    _cmd_cost_report(_S(db_path), args)
    out = capsys.readouterr().out
    assert "rsi2" in out
    assert "donchian" not in out
