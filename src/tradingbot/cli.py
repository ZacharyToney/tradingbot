from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from tradingbot.config import REPO_ROOT, load_settings
from tradingbot.logging_setup import configure_logging

STRATEGY_REGISTRY = {}


def _register_strategies() -> None:
    """Lazy registration to keep CLI startup fast and avoid import-time side effects."""
    from tradingbot.strategies.donchian_breakout import DonchianBreakout
    from tradingbot.strategies.rsi2_mean_reversion import RSI2MeanReversion

    STRATEGY_REGISTRY["rsi2"] = RSI2MeanReversion
    STRATEGY_REGISTRY["donchian"] = DonchianBreakout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tb", description="tradingBot CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print account snapshot + open positions")
    sub.add_parser("halt", help="Touch HALT file to stop a running loop")
    sub.add_parser("reconcile", help="Reconcile local DB state against broker")

    cr = sub.add_parser(
        "cost-report",
        help="Aggregate realized slippage + in-kind fees from the orders table",
    )
    cr.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD inclusive")
    cr.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD inclusive")
    cr.add_argument("--strategy", default=None, help="Filter to one strategy (rsi2|donchian)")

    bt = sub.add_parser("backtest", help="Run a backtest")
    bt.add_argument("strategy", choices=["rsi2", "donchian"], help="Strategy name")
    bt.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD inclusive")
    bt.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD inclusive")
    bt.add_argument("--symbols", required=True, help="Comma-separated, e.g. SPY,QQQ")
    bt.add_argument("--equity", type=float, default=100_000.0)
    bt.add_argument("--slippage-bps", type=float, default=5.0)

    wf = sub.add_parser("walkforward", help="Walk-forward parameter optimization")
    wf.add_argument("strategy", choices=["rsi2", "donchian"])
    wf.add_argument("--from", dest="start", required=True)
    wf.add_argument("--to", dest="end", required=True)
    wf.add_argument("--symbols", required=True)
    wf.add_argument("--train-months", type=int, default=24)
    wf.add_argument("--test-months", type=int, default=6)
    wf.add_argument("--roll-months", type=int, default=6)
    wf.add_argument(
        "--objective", choices=["sharpe", "cagr", "profit_factor"], default="sharpe"
    )
    wf.add_argument("--equity", type=float, default=100_000.0)

    lv = sub.add_parser("live", help="Run the live paper-trading loop")
    mode = lv.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Use Alpaca paper account (default)")
    mode.add_argument(
        "--live", action="store_true", help="Use Alpaca live account (requires safety flag)"
    )
    lv.add_argument(
        "--i-really-mean-live",
        action="store_true",
        help="Safety flag required to actually use --live (real money)",
    )
    lv.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit orders. Without this, runs in dry-run mode (no orders).",
    )
    lv.add_argument(
        "--strategies",
        default="rsi2",
        help="Comma-separated list of strategy names to run (default: rsi2)",
    )

    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings)

    if args.cmd == "status":
        return _cmd_status(settings)
    if args.cmd == "halt":
        return _cmd_halt()
    if args.cmd == "reconcile":
        return _cmd_reconcile(settings)
    if args.cmd == "cost-report":
        return _cmd_cost_report(settings, args)
    if args.cmd == "backtest":
        return _cmd_backtest(settings, args)
    if args.cmd == "walkforward":
        return _cmd_walkforward(settings, args)
    if args.cmd == "live":
        return _cmd_live(settings, args)
    return 0


def _cmd_status(settings) -> int:
    from tradingbot.execution.broker import AlpacaBroker

    broker = AlpacaBroker(settings)
    acc = broker.get_account()
    positions = broker.get_positions()
    print(
        f"mode={settings.trading_mode} equity={acc.equity:.2f} cash={acc.cash:.2f} "
        f"buying_power={acc.buying_power:.2f} daytrades={acc.daytrade_count}"
    )
    for p in positions:
        print(
            f"  {p.symbol:<10} qty={p.qty:>10.4f} entry={p.avg_entry_price:.4f} "
            f"mv={p.market_value:.2f}"
        )
    return 0


def _cmd_halt() -> int:
    halt = Path(REPO_ROOT) / "HALT"
    halt.touch()
    print(f"touched {halt}")
    return 0


def _cmd_backtest(settings, args) -> int:
    from tradingbot.backtest.engine import run_backtest
    from tradingbot.backtest.metrics import compute_metrics
    from tradingbot.backtest.report import write_report
    from tradingbot.data.bars import BarSource
    from tradingbot.data.cache import load_bars

    _register_strategies()
    if args.strategy not in STRATEGY_REGISTRY:
        print(f"unknown strategy: {args.strategy}", file=sys.stderr)
        return 2

    StrategyCls = STRATEGY_REGISTRY[args.strategy]
    strategy = StrategyCls()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    source = BarSource(settings)
    cache_dir = REPO_ROOT / "data" / "cache"
    bars = {}
    for symbol in symbols:
        logger.info(f"loading bars symbol={symbol}")
        df = load_bars(source, symbol, strategy.timeframe, start, end, cache_dir)
        if df.empty:
            logger.warning(f"no bars for {symbol}, skipping")
            continue
        bars[symbol] = df

    if not bars:
        print("no bars available for any symbol", file=sys.stderr)
        return 3

    logger.info(
        f"running backtest strategy={strategy.name} symbols={list(bars)} equity={args.equity}"
    )
    result = run_backtest(
        strategy,
        bars,
        starting_equity=args.equity,
        slippage_bps=args.slippage_bps,
    )
    metrics = compute_metrics(result.equity_curve, result.trades)

    reports_root = REPO_ROOT / "backtest_reports"
    out_dir = write_report(
        result,
        metrics,
        reports_root,
        extra={
            "symbols": ",".join(bars.keys()),
            "from": args.start,
            "to": args.end,
            "starting_equity": f"{args.equity:.2f}",
            "slippage_bps": f"{args.slippage_bps:.2f}",
        },
    )

    print(f"\n=== {strategy.name} backtest ===")
    print(f"symbols:        {','.join(bars.keys())}")
    print(f"period:         {args.start} → {args.end}")
    print(f"trade_count:    {metrics.trade_count}")
    print(f"win_rate:       {metrics.win_rate:.2%}")
    print(f"profit_factor:  {metrics.profit_factor:.3f}")
    print(f"sharpe (ann.):  {metrics.sharpe:.3f}")
    print(f"sortino (ann.): {metrics.sortino:.3f}")
    print(f"max_drawdown:   {metrics.max_dd:.2%}")
    print(f"cagr:           {metrics.cagr:.2%}")
    if len(result.equity_curve) >= 2:
        ret = result.equity_curve.iloc[-1] / result.equity_curve.iloc[0] - 1.0
        print(f"total_return:   {ret:.2%}")
    print(f"report:         {out_dir}")
    return 0


def _cmd_walkforward(settings, args) -> int:
    from tradingbot.backtest.walkforward import ParamGrid, walk_forward, write_report
    from tradingbot.data.bars import BarSource
    from tradingbot.data.cache import load_bars

    _register_strategies()
    StrategyCls = STRATEGY_REGISTRY[args.strategy]

    # Param grid per strategy. Small and conservative.
    if args.strategy == "rsi2":
        grid = ParamGrid(
            strategy="rsi2",
            grid={
                "rsi_buy_threshold": [5.0, 10.0, 15.0],
                "rsi_sell_threshold": [60.0, 70.0, 80.0],
                "max_hold_bars": [3, 5, 7],
                "trend_filter_window": [100, 150, 200],
            },
        )
    else:  # donchian
        grid = ParamGrid(
            strategy="donchian",
            grid={
                "enter_window": [10, 20, 30, 55],
                "exit_window": [5, 10, 20],
            },
        )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    source = BarSource(settings)
    cache_dir = REPO_ROOT / "data" / "cache"
    bars: dict = {}
    # Use the strategy's natural timeframe (TF(1, "Day")).
    tf = StrategyCls().timeframe
    for symbol in symbols:
        logger.info(f"loading bars symbol={symbol}")
        df = load_bars(source, symbol, tf, start, end, cache_dir)
        if df.empty:
            logger.warning(f"no bars for {symbol}, skipping")
            continue
        bars[symbol] = df

    if not bars:
        print("no bars available for any symbol", file=sys.stderr)
        return 3

    logger.info(
        f"walkforward strategy={args.strategy} train={args.train_months}mo "
        f"test={args.test_months}mo roll={args.roll_months}mo objective={args.objective}"
    )
    result = walk_forward(
        strategy_factory=StrategyCls,
        grid=grid,
        bars=bars,
        train_months=args.train_months,
        test_months=args.test_months,
        roll_months=args.roll_months,
        objective=args.objective,
        starting_equity=args.equity,
    )

    out_dir = write_report(
        result,
        REPO_ROOT / "walkforward_reports",
        extra={
            "strategy": args.strategy,
            "symbols": ",".join(bars.keys()),
            "from": args.start,
            "to": args.end,
            "train_months": str(args.train_months),
            "test_months": str(args.test_months),
            "roll_months": str(args.roll_months),
            "objective": args.objective,
        },
    )

    print(f"\n=== {args.strategy} walk-forward ===")
    print(f"windows: {len(result.windows)}")
    if result.windows:
        is_sharpe = [w.train_metrics.sharpe for w in result.windows]
        oos_sharpe = [w.test_metrics.sharpe for w in result.windows]
        is_mean = sum(is_sharpe) / len(is_sharpe)
        oos_mean = sum(oos_sharpe) / len(oos_sharpe)
        print(f"in-sample  Sharpe mean: {is_mean:.3f}")
        print(f"out-of-sample Sharpe mean: {oos_mean:.3f}")
        flag = result.overfit_flag()
        if flag:
            print("⚠  OVERFIT FLAG: OOS < 50% of IS — strategy may not generalize")
        else:
            print("✓  OOS ≥ 50% of IS — strategy generalizes acceptably")
        print("\nper-window best params:")
        for w in result.windows:
            print(
                f"  {w.test_start.date()}–{w.test_end.date()} "
                f"params={w.best_params} oos_sharpe={w.test_metrics.sharpe:.3f}"
            )
    print(f"report: {out_dir}")
    return 0


def _cmd_reconcile(settings) -> int:
    """Compare bot-tracked positions (sum of fills per strategy) against broker positions."""
    from tradingbot.db import connect
    from tradingbot.execution.broker import AlpacaBroker

    db = connect(settings.db_full_path)
    broker = AlpacaBroker(settings)

    rows = db.execute(
        """SELECT o.strategy,
                  f.symbol,
                  SUM(CASE WHEN f.side='buy' THEN f.qty ELSE -f.qty END) AS net_qty
           FROM fills f JOIN orders o ON f.client_order_id = o.client_order_id
           GROUP BY o.strategy, f.symbol HAVING net_qty != 0"""
    ).fetchall()
    bot_positions = [(r["strategy"], r["symbol"], r["net_qty"]) for r in rows]

    from tradingbot.execution.broker import canon_symbol

    broker_positions = {p.symbol: p.qty for p in broker.get_positions()}

    print(f"bot-tracked positions ({len(bot_positions)}):")
    for strat, sym, qty in bot_positions:
        print(f"  {strat:>10} {sym:<10} qty={qty:.4f}")

    print(f"\nbroker positions ({len(broker_positions)}):")
    for sym, qty in broker_positions.items():
        print(f"  {sym:<10} qty={qty:.4f}")

    bot_sums: dict[str, float] = {}
    for _, sym, qty in bot_positions:
        bot_sums[canon_symbol(sym)] = bot_sums.get(canon_symbol(sym), 0.0) + qty
    broker_sums = {canon_symbol(s): q for s, q in broker_positions.items()}

    # Tolerate up to 1% absolute difference for crypto (fees + slippage on fills mean
    # actual broker qty is slightly less than our recorded fill qty).
    drift = []
    for sym in set(bot_sums) | set(broker_sums):
        b = bot_sums.get(sym, 0.0)
        br = broker_sums.get(sym, 0.0)
        tol = max(0.01 * max(abs(b), abs(br)), 1e-6)
        if abs(b - br) > tol:
            drift.append((sym, b, br))

    if drift:
        print("\nDRIFT DETECTED:")
        for sym, b, br in drift:
            print(f"  {sym}: bot={b:.4f} broker={br:.4f} delta={br-b:+.4f}")
        return 1
    print("\nno drift")
    return 0


def _cmd_cost_report(settings, args) -> int:
    """Aggregate realized slippage + in-kind fees from the orders table.

    Output: per-(strategy, asset_class) median + p90 slippage (bps), total fee in $,
    plus a one-line gap-vs-5bps-assumption summary.
    """
    from statistics import median, quantiles

    from tradingbot.db import connect

    db = connect(settings.db_full_path)

    where_clauses = ["realized_slippage_bps IS NOT NULL"]
    params: list = []
    if args.start:
        start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp() * 1000)
        where_clauses.append("submitted_at_ms >= ?")
        params.append(start_ms)
    if args.end:
        end_ms = int(datetime.fromisoformat(args.end).replace(tzinfo=UTC).timestamp() * 1000)
        # Inclusive end-of-day
        end_ms += 24 * 60 * 60 * 1000
        where_clauses.append("submitted_at_ms < ?")
        params.append(end_ms)
    if args.strategy:
        where_clauses.append("strategy = ?")
        params.append(args.strategy)

    sql = (
        "SELECT strategy, symbol, side, realized_slippage_bps, fee_qty, qty, intended_price "
        f"FROM orders WHERE {' AND '.join(where_clauses)}"
    )
    rows = db.execute(sql, params).fetchall()

    if not rows:
        print("no filled orders with measured slippage in this range")
        return 0

    # Group by (strategy, asset_class).
    def _asset_class(symbol: str) -> str:
        return "crypto" if "/" in symbol else "equity"

    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["strategy"], _asset_class(r["symbol"]))
        g = groups.setdefault(key, {"slip": [], "fee_usd": 0.0, "n": 0})
        g["slip"].append(float(r["realized_slippage_bps"]))
        # fee_qty is in base asset for crypto. Multiply by intended price for $ value.
        if r["fee_qty"] and r["intended_price"]:
            g["fee_usd"] += float(r["fee_qty"]) * float(r["intended_price"])
        g["n"] += 1

    print("\n=== cost report ===")
    print(f"{'strategy':<12}{'asset':<8}{'n':>5}{'median_bps':>14}{'p90_bps':>12}{'fee_$':>12}")
    print("-" * 65)
    weighted_median: list[float] = []
    for (strat, asset), g in sorted(groups.items()):
        slips = g["slip"]
        med = median(slips)
        # statistics.quantiles needs n >= 2. For n=1, p90 collapses to the only value.
        p90 = quantiles(slips, n=10, method="inclusive")[8] if len(slips) >= 2 else slips[0]
        print(
            f"{strat:<12}{asset:<8}{g['n']:>5}{med:>14.1f}{p90:>12.1f}{g['fee_usd']:>12.2f}"
        )
        weighted_median.extend(slips)

    if weighted_median:
        overall = median(weighted_median)
        gap = overall - 5.0
        print(
            f"\noverall median slippage: {overall:.1f} bps "
            f"(backtest assumes 5.0 bps; gap {gap:+.1f})"
        )
    return 0


def _cmd_live(settings, args) -> int:
    from tradingbot.data.bars import BarSource
    from tradingbot.db import connect
    from tradingbot.execution.broker import AlpacaBroker
    from tradingbot.live.loop import LiveContext, run_loop

    use_live = bool(args.live)
    if use_live:
        if not args.i_really_mean_live:
            print("--live requires --i-really-mean-live", file=sys.stderr)
            return 2
        if settings.trading_mode != "live":
            print(
                "--live requested but TRADING_MODE in .env is 'paper'. Refusing.",
                file=sys.stderr,
            )
            return 2

    _register_strategies()
    strategy_names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    strategies = []
    for name in strategy_names:
        if name not in STRATEGY_REGISTRY:
            print(f"unknown strategy: {name}", file=sys.stderr)
            return 2
        strategies.append(STRATEGY_REGISTRY[name]())

    db = connect(settings.db_full_path)
    broker = AlpacaBroker(settings)
    bar_source = BarSource(settings)

    universe = sorted({sym for s in strategies for sym in s.universe})
    banner = (
        f"\n{'='*60}\n"
        f"tradingBot live loop\n"
        f"  mode:              {settings.trading_mode}\n"
        f"  execute:           {args.execute}\n"
        f"  strategies:        {[s.name for s in strategies]}\n"
        f"  universe:          {universe}\n"
        f"  risk caps:         pos_pct={settings.max_position_pct} "
        f"concur={settings.max_concurrent_positions} "
        f"daily_loss={settings.daily_loss_limit_pct} "
        f"total_dd={settings.total_dd_limit_pct}\n"
        f"  poll_interval:     {settings.poll_interval_seconds}s\n"
        f"  HALT file:         {REPO_ROOT}/HALT (touch to stop)\n"
        f"{'='*60}\n"
    )
    print(banner)
    logger.info(f"live loop start mode={settings.trading_mode} execute={args.execute}")

    ctx = LiveContext(
        settings=settings,
        db=db,
        broker=broker,
        bar_source=bar_source,
        strategies=strategies,
        repo_root=REPO_ROOT,
        dry_run=not args.execute,
    )
    return run_loop(ctx)


if __name__ == "__main__":
    sys.exit(main())
