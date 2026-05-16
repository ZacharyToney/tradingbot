"""Each circuit breaker gets a focused unit test. The composition into
`gates.pre_trade` is tested separately in `tests/test_gates.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tradingbot.risk.limits import (
    Decision,
    check_clock_skew,
    check_daily_loss,
    check_equity_drift,
    check_extended_hours,
    check_halt_file,
    check_long_only_equities,
    check_max_concurrent,
    check_max_position_pct,
    check_pdt_violation,
    check_total_drawdown,
)


def test_decision_dataclass():
    d = Decision(allow=True)
    assert d.allow is True
    assert d.reason == ""
    d2 = Decision(allow=False, reason="nope")
    assert d2.allow is False and d2.reason == "nope"


# ---- max_position_pct ------------------------------------------------------

def test_max_position_pct_under_cap_allows():
    d = check_max_position_pct(order_value=4_999.0, equity=100_000.0, max_pct=0.05)
    assert d.allow


def test_max_position_pct_at_cap_allows():
    d = check_max_position_pct(order_value=5_000.0, equity=100_000.0, max_pct=0.05)
    assert d.allow


def test_max_position_pct_over_cap_rejects():
    d = check_max_position_pct(order_value=5_001.0, equity=100_000.0, max_pct=0.05)
    assert not d.allow
    assert "position" in d.reason.lower()


# ---- max_concurrent --------------------------------------------------------

def test_max_concurrent_under_limit_allows():
    d = check_max_concurrent(current_open=2, max_concurrent=3)
    assert d.allow


def test_max_concurrent_at_limit_rejects():
    # If we're already at the limit, opening a NEW position would push us over.
    d = check_max_concurrent(current_open=3, max_concurrent=3)
    assert not d.allow


# ---- daily_loss -----------------------------------------------------------

def test_daily_loss_within_tolerance_allows():
    d = check_daily_loss(
        equity_now=99_500.0,         # -0.5% intraday
        equity_start_of_day=100_000.0,
        limit_pct=0.02,
    )
    assert d.allow


def test_daily_loss_breached_rejects():
    d = check_daily_loss(
        equity_now=97_500.0,         # -2.5% intraday — past -2% halt
        equity_start_of_day=100_000.0,
        limit_pct=0.02,
    )
    assert not d.allow
    assert "daily" in d.reason.lower()


# ---- total_drawdown -------------------------------------------------------

def test_total_drawdown_within_tolerance_allows():
    d = check_total_drawdown(equity_now=95_000.0, equity_all_time_high=100_000.0, limit_pct=0.10)
    assert d.allow


def test_total_drawdown_breached_rejects():
    d = check_total_drawdown(equity_now=89_000.0, equity_all_time_high=100_000.0, limit_pct=0.10)
    assert not d.allow
    assert "drawdown" in d.reason.lower()


# ---- clock_skew -----------------------------------------------------------

def test_clock_skew_small_allows():
    d = check_clock_skew(skew_seconds=2.0, max_skew_seconds=5)
    assert d.allow


def test_clock_skew_large_rejects():
    d = check_clock_skew(skew_seconds=12.0, max_skew_seconds=5)
    assert not d.allow
    assert "clock" in d.reason.lower()


def test_clock_skew_unknown_rejects():
    d = check_clock_skew(skew_seconds=float("inf"), max_skew_seconds=5)
    assert not d.allow


# ---- equity_drift ---------------------------------------------------------

def test_equity_drift_within_tolerance_allows():
    d = check_equity_drift(broker_equity=100_300.0, last_recon_equity=100_000.0, max_drift_pct=0.01)
    assert d.allow


def test_equity_drift_over_threshold_rejects():
    d = check_equity_drift(broker_equity=102_000.0, last_recon_equity=100_000.0, max_drift_pct=0.01)
    assert not d.allow
    assert "drift" in d.reason.lower()


def test_equity_drift_no_prior_snapshot_allows():
    """First run: no previous snapshot → cannot have drifted."""
    d = check_equity_drift(broker_equity=100_000.0, last_recon_equity=None, max_drift_pct=0.01)
    assert d.allow


# ---- pdt_violation --------------------------------------------------------

def test_pdt_under_25k_third_daytrade_allowed():
    d = check_pdt_violation(daytrades_in_5d=3, equity=10_000.0)
    assert d.allow


def test_pdt_under_25k_fourth_daytrade_rejected():
    d = check_pdt_violation(daytrades_in_5d=4, equity=10_000.0)
    assert not d.allow
    assert "pdt" in d.reason.lower() or "day-trade" in d.reason.lower()


def test_pdt_over_25k_unrestricted():
    d = check_pdt_violation(daytrades_in_5d=20, equity=100_000.0)
    assert d.allow


# ---- halt_file -----------------------------------------------------------

def test_halt_file_absent_allows(tmp_path: Path):
    d = check_halt_file(repo_root=tmp_path)
    assert d.allow


def test_halt_file_present_rejects(tmp_path: Path):
    (tmp_path / "HALT").touch()
    d = check_halt_file(repo_root=tmp_path)
    assert not d.allow
    assert "halt" in d.reason.lower()


# ---- extended_hours ------------------------------------------------------

def test_extended_hours_crypto_always_allowed():
    # Crypto: 3 AM UTC on a Sunday — totally outside RTH but crypto trades 24/7
    sunday_3am_utc = datetime(2025, 5, 18, 3, 0, tzinfo=UTC)
    d = check_extended_hours(now=sunday_3am_utc, asset_class="crypto")
    assert d.allow


def test_extended_hours_equity_during_rth_allowed():
    # Tuesday 15:00 UTC = 11:00 ET (RTH 9:30-16:00 ET)
    tue_rth = datetime(2025, 5, 13, 15, 0, tzinfo=UTC)
    d = check_extended_hours(now=tue_rth, asset_class="equity")
    assert d.allow


def test_extended_hours_equity_outside_rth_rejected():
    # Tuesday 02:00 UTC = previous-day 22:00 ET (overnight)
    tue_overnight = datetime(2025, 5, 13, 2, 0, tzinfo=UTC)
    d = check_extended_hours(now=tue_overnight, asset_class="equity")
    assert not d.allow
    assert "hours" in d.reason.lower()


def test_extended_hours_equity_weekend_rejected():
    # Saturday — market closed
    saturday = datetime(2025, 5, 17, 15, 0, tzinfo=UTC)
    d = check_extended_hours(now=saturday, asset_class="equity")
    assert not d.allow


# ---- long_only_equities --------------------------------------------------

def test_long_only_equities_buy_allowed():
    d = check_long_only_equities(symbol="SPY", side="buy", asset_class="equity")
    assert d.allow


def test_long_only_equities_sell_to_close_allowed():
    """Selling an existing long is fine — sell only forbidden when it would open a short."""
    d = check_long_only_equities(symbol="SPY", side="sell", asset_class="equity", current_qty=10)
    assert d.allow


def test_long_only_equities_sell_to_short_rejected():
    d = check_long_only_equities(symbol="SPY", side="sell", asset_class="equity", current_qty=0)
    assert not d.allow
    assert "long-only" in d.reason.lower() or "short" in d.reason.lower()


def test_long_only_equities_crypto_short_allowed():
    """Crypto allows shorting in our v1 design."""
    d = check_long_only_equities(symbol="BTC/USD", side="sell", asset_class="crypto", current_qty=0)
    assert d.allow
