from __future__ import annotations

from tradingbot.execution.reconciler import reconcile


def _settings_kw(**kw):
    base = dict(
        max_position_pct=0.05,
        max_concurrent_positions=3,
        equity_for_sizing=100_000.0,
    )
    base.update(kw)
    return base


def test_flat_to_long_emits_single_buy():
    orders = reconcile(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 0.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert len(orders) == 1
    o = orders[0]
    assert o.symbol == "SPY"
    assert o.side == "buy"
    assert o.qty == 50.0   # 5% of $100k = $5k / $100 = 50 shares
    assert o.strategy == "rsi2"
    assert o.client_order_id  # non-empty
    assert o.bar_ts_ms == 1_700_000_000_000


def test_long_to_flat_emits_single_sell():
    orders = reconcile(
        targets={"SPY": 0.0},
        bot_positions={"SPY": 50.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert len(orders) == 1
    assert orders[0].side == "sell"
    assert orders[0].qty == 50.0


def test_noop_when_target_matches_current():
    orders = reconcile(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 50.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert orders == []


def test_partial_top_up():
    """Currently holding 30 shares but target says 50 — emit a buy for 20."""
    orders = reconcile(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 30.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].qty == 20.0


def test_partial_close():
    """Currently 50 but target down to 0.5 (half) → sell 25."""
    orders = reconcile(
        targets={"SPY": 0.5},
        bot_positions={"SPY": 50.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert len(orders) == 1
    assert orders[0].side == "sell"
    assert orders[0].qty == 25.0


def test_multi_symbol_independent_diffs():
    orders = reconcile(
        targets={"SPY": 1.0, "QQQ": 0.0, "IWM": 1.0},
        bot_positions={"SPY": 0.0, "QQQ": 30.0, "IWM": 0.0},
        prices={"SPY": 100.0, "QQQ": 100.0, "IWM": 50.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity", "QQQ": "equity", "IWM": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    by_symbol = {o.symbol: o for o in orders}
    assert set(by_symbol) == {"SPY", "QQQ", "IWM"}
    assert by_symbol["SPY"].side == "buy" and by_symbol["SPY"].qty == 50.0
    assert by_symbol["QQQ"].side == "sell" and by_symbol["QQQ"].qty == 30.0
    assert by_symbol["IWM"].side == "buy" and by_symbol["IWM"].qty == 100.0  # 5k / 50


def test_ignores_symbols_not_in_targets():
    """Broker shows a position the strategy doesn't manage — reconciler leaves it alone."""
    orders = reconcile(
        targets={"SPY": 1.0},                       # strategy only knows SPY
        bot_positions={"SPY": 0.0, "TSLA": 20.0},   # there's also a TSLA position (not ours)
        prices={"SPY": 100.0, "TSLA": 300.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity", "TSLA": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert len(orders) == 1
    assert orders[0].symbol == "SPY"


def test_dust_difference_no_order():
    """Tiny mismatch under 1 share should not emit an order."""
    orders = reconcile(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 49.5},   # target 50, hold 49.5, diff 0.5 — under dust
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    assert orders == []


def test_idempotency_keys_deterministic():
    """Same inputs → same client_order_id."""
    common = dict(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 0.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
        bar_ts_ms=1_700_000_000_000,
    )
    o1 = reconcile(**common)[0]
    o2 = reconcile(**common)[0]
    assert o1.client_order_id == o2.client_order_id


def test_idempotency_keys_differ_per_bar():
    common = dict(
        targets={"SPY": 1.0},
        bot_positions={"SPY": 0.0},
        prices={"SPY": 100.0},
        equity_for_sizing=100_000.0,
        max_position_pct=0.05,
        asset_classes={"SPY": "equity"},
        strategy_name="rsi2",
    )
    o1 = reconcile(**common, bar_ts_ms=1_700_000_000_000)[0]
    o2 = reconcile(**common, bar_ts_ms=1_700_000_086_400)[0]   # +1 day
    assert o1.client_order_id != o2.client_order_id
