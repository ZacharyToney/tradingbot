from __future__ import annotations

import pytest

from tradingbot.risk.sizing import size_position


def test_long_position_returns_positive_whole_shares():
    qty = size_position(
        target_weight=1.0,
        equity_for_sizing=100_000.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="equity",
    )
    # 5% of $100k = $5k; floor(5000 / 50) = 100 shares
    assert qty == 100


def test_short_position_returns_negative_whole_shares_when_allowed():
    qty = size_position(
        target_weight=-1.0,
        equity_for_sizing=100_000.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="crypto",
    )
    assert qty < 0
    assert qty == -100


def test_weight_under_one_scales_proportionally():
    qty = size_position(
        target_weight=0.5,           # half the max weight → 2.5% of equity
        equity_for_sizing=100_000.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="equity",
    )
    # 2.5% of $100k = $2,500; floor(2500/50) = 50 shares
    assert qty == 50


def test_weight_clipped_to_one():
    # weight = 2.0 should be clipped to 1.0; final size = 5% of equity, not 10%
    qty = size_position(
        target_weight=2.0,
        equity_for_sizing=100_000.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="equity",
    )
    assert qty == 100  # same as target_weight=1.0


def test_zero_weight_returns_zero():
    qty = size_position(
        target_weight=0.0,
        equity_for_sizing=100_000.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="equity",
    )
    assert qty == 0


def test_equity_dust_returns_zero_for_equity():
    # 5% of $10 = $0.50 — less than one share at $50 → 0
    qty = size_position(
        target_weight=1.0,
        equity_for_sizing=10.0,
        price=50.0,
        max_position_pct=0.05,
        asset_class="equity",
    )
    assert qty == 0


def test_crypto_fractional_qty():
    qty = size_position(
        target_weight=1.0,
        equity_for_sizing=10_000.0,
        price=30_000.0,
        max_position_pct=0.05,
        asset_class="crypto",
    )
    # 5% of $10k = $500; $500 / $30,000 = 0.01666... BTC
    assert qty == pytest.approx(500.0 / 30_000.0, abs=1e-8)


def test_crypto_dust_returns_zero():
    # 5% of $1 = $0.05 — too small to bother
    qty = size_position(
        target_weight=1.0,
        equity_for_sizing=1.0,
        price=30_000.0,
        max_position_pct=0.05,
        asset_class="crypto",
    )
    assert qty == 0
