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


def test_cost_report_p90_uses_proper_percentile_for_small_n(tmp_path: Path, capsys):
    """Regression for the 2026-05-20 cost-report bug: with n=3 the hand-rolled index
    `int(0.9 * n) - 1` returned the wrong value (-32.2 for the IWM/AMZN/GOOGL case
    when p90 should be near the +87.9 GOOGL outlier). statistics.quantiles fixes it."""
    db_path = tmp_path / "tb.db"
    db = connect(db_path)
    # Same three values that surfaced the bug in production.
    _insert_filled_order(db, "a", "rsi2", "IWM", "buy", 18.0, -32.2)
    _insert_filled_order(db, "b", "rsi2", "AMZN", "sell", 18.0, -43.4)
    _insert_filled_order(db, "c", "rsi2", "GOOGL", "buy", 12.0, 87.9)
    db.close()

    _cmd_cost_report(_S(db_path), _Args())
    out = capsys.readouterr().out
    # statistics.quantiles for [−43.4, −32.2, 87.9] at 90th pct (inclusive method) ≈ 63.9.
    # Old buggy code returned -32.2. We accept anything >= ~50 as "obviously not the old bug."
    lines = [ln for ln in out.splitlines() if "rsi2" in ln and "equity" in ln]
    assert lines, f"no rsi2/equity line in output: {out}"
    # Columns are right-aligned: ... median_bps  p90_bps  fee_$
    parts = lines[0].split()
    p90 = float(parts[-2])
    assert p90 > 50, f"p90 looks like the old bug: {p90} (full line: {lines[0]})"


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
