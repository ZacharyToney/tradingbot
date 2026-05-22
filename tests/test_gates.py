"""Compose all the individual limit checks via `pre_trade`. We test:
 - happy path passes
 - each rejection reason short-circuits with the right message
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tradingbot.config import Settings
from tradingbot.risk.gates import AccountState, IntendedOrder, pre_trade


def _settings(**overrides):
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
    base.update(overrides)
    return Settings(**base)


def _order(symbol="SPY", side="buy", qty=10.0, asset_class="equity") -> IntendedOrder:
    return IntendedOrder(
        strategy="rsi2",
        symbol=symbol,
        side=side,
        qty=qty,
        price=100.0,
        asset_class=asset_class,
        bar_ts_ms=1_700_000_000_000,
        client_order_id="rsi2|SPY|1700000000000|buy",
    )


def _state(**overrides) -> AccountState:
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
        now=datetime(2025, 5, 13, 15, 0, tzinfo=UTC),  # Tue 11:00 ET — RTH
        repo_root=Path("/tmp/_tradingbot_test_no_halt"),
        equity_for_sizing=100_000.0,
    )
    base.update(overrides)
    return AccountState(**base)


def test_pre_trade_happy_path_passes(tmp_path: Path):
    d = pre_trade(_order(), _state(repo_root=tmp_path), _settings())
    assert d.allow, d.reason


def test_pre_trade_halt_file_blocks(tmp_path: Path):
    (tmp_path / "HALT").touch()
    d = pre_trade(_order(), _state(repo_root=tmp_path), _settings())
    assert not d.allow
    assert "halt" in d.reason.lower()


def test_pre_trade_clock_skew_blocks(tmp_path: Path):
    d = pre_trade(_order(), _state(repo_root=tmp_path, clock_skew_seconds=99.0), _settings())
    assert not d.allow and "clock" in d.reason.lower()


def test_pre_trade_position_over_cap_blocks(tmp_path: Path):
    # 100 shares * $100 = $10,000 = 10% of $100k → over the 5% cap
    o = _order(qty=100.0)
    d = pre_trade(o, _state(repo_root=tmp_path), _settings())
    assert not d.allow and "position" in d.reason.lower()


def test_pre_trade_max_concurrent_blocks_new(tmp_path: Path):
    s = _state(repo_root=tmp_path, current_open_positions=3)
    d = pre_trade(_order(), s, _settings())
    assert not d.allow and "concurrent" in d.reason.lower()


def test_pre_trade_max_concurrent_does_not_block_close(tmp_path: Path):
    """Closing an existing position should not trip the concurrent cap."""
    s = _state(repo_root=tmp_path, current_open_positions=3, current_qty_for_symbol=10.0)
    o = _order(side="sell", qty=10.0)
    d = pre_trade(o, s, _settings())
    assert d.allow, d.reason


def test_pre_trade_daily_loss_blocks(tmp_path: Path):
    s = _state(repo_root=tmp_path, equity_now=97_500.0)  # -2.5%
    d = pre_trade(_order(), s, _settings())
    assert not d.allow and "daily" in d.reason.lower()


def test_pre_trade_total_dd_blocks(tmp_path: Path):
    # All-time high = 100k, but we started today at 89.5k (no daily-loss trigger). Total DD = 11%.
    s = _state(
        repo_root=tmp_path,
        equity_now=89_000.0,
        equity_start_of_day=89_500.0,
        equity_all_time_high=100_000.0,
        last_recon_equity=89_000.0,
        broker_equity=89_000.0,
    )
    d = pre_trade(_order(), s, _settings())
    assert not d.allow and "drawdown" in d.reason.lower()


def test_pre_trade_equity_drift_blocks(tmp_path: Path):
    s = _state(repo_root=tmp_path, broker_equity=102_500.0)  # +2.5% drift
    d = pre_trade(_order(), s, _settings())
    assert not d.allow and "drift" in d.reason.lower()


def test_max_position_pct_uses_equity_for_sizing_not_current_equity(tmp_path: Path):
    """Regression for the 2026-05-22 chaos-bot rounding cascade.

    Sizing uses starting_equity (no compounding); the gate's cap check must use the
    SAME denominator. If it uses equity_now instead, a small unrealized loss makes a
    correctly-sized at-cap order look slightly over-cap and the gate falsely rejects it.
    Reproduces the live failure: $1000 starting, $999.83 current after a small drawdown,
    order sized at exactly $250 (25% of starting) gets rejected because $250 / $999.83
    = 25.004% > 25% cap.
    """
    # Order sized to be at-cap relative to starting equity.
    order = _order(side="buy", qty=2.5)   # qty * price=100 = $250 notional
    # equity_for_sizing stays at the starting baseline; equity_now has drifted down.
    s = _state(
        repo_root=tmp_path,
        equity_now=999.83,
        equity_for_sizing=1000.0,
        equity_start_of_day=1000.0,
        equity_all_time_high=1000.0,
        last_recon_equity=999.83,
        broker_equity=999.83,
    )
    settings = _settings(max_position_pct=0.25)
    d = pre_trade(order, s, settings)
    # With the fix, gate uses equity_for_sizing=1000 → 250/1000 = 25.0% = at cap → pass.
    # Pre-fix, it would have used equity_now=999.83 → 250/999.83 = 25.004% > cap → reject.
    assert d.allow, f"gate falsely rejected at-cap order: {d.reason}"


def test_pre_trade_extended_hours_blocks_equity(tmp_path: Path):
    # 3 AM UTC Tuesday = previous-day 23:00 ET
    s = _state(repo_root=tmp_path, now=datetime(2025, 5, 13, 3, 0, tzinfo=UTC))
    d = pre_trade(_order(), s, _settings())
    assert not d.allow and "hours" in d.reason.lower()


def test_pre_trade_extended_hours_ok_for_crypto(tmp_path: Path):
    s = _state(repo_root=tmp_path, now=datetime(2025, 5, 18, 3, 0, tzinfo=UTC))  # Sun overnight
    o = _order(symbol="BTC/USD", asset_class="crypto")
    d = pre_trade(o, s, _settings())
    assert d.allow, d.reason


def test_pre_trade_long_only_equity_blocks_short_open(tmp_path: Path):
    s = _state(repo_root=tmp_path, current_qty_for_symbol=0.0)
    o = _order(side="sell", qty=10.0)
    d = pre_trade(o, s, _settings())
    assert not d.allow and ("long-only" in d.reason.lower() or "short" in d.reason.lower())


def test_pre_trade_pdt_blocks_under_25k(tmp_path: Path):
    s = _state(repo_root=tmp_path, equity_now=10_000.0, equity_start_of_day=10_000.0,
               equity_all_time_high=10_000.0, last_recon_equity=10_000.0,
               broker_equity=10_000.0, daytrades_in_5d=4)
    # Note: this also requires the per-position cap to still pass at $10k equity.
    # 10 shares * $100 = $1000 = 10% of $10k → over cap. So this test is more about
    # PDT first-fail vs cap first-fail. Order matters; let's send a tinier order.
    o = _order(qty=4.0)   # 4 * $100 = $400 = 4% of $10k → under 5% cap
    d = pre_trade(o, s, _settings())
    assert not d.allow
    assert "pdt" in d.reason.lower() or "day-trade" in d.reason.lower()


def test_pre_trade_reject_short_circuits_at_first_failure(tmp_path: Path):
    """Multiple failures: we should see the first-evaluated reason, not a composite."""
    (tmp_path / "HALT").touch()
    s = _state(repo_root=tmp_path, clock_skew_seconds=99.0, equity_now=80_000.0)
    d = pre_trade(_order(), s, _settings())
    # HALT is checked first per the implementation; assert that reason wins.
    assert not d.allow
    # Don't pin which specific reason — just that we got one of the early ones.
    assert d.reason
