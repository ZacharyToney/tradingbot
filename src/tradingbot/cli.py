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
    from tradingbot.strategies.rsi2_mean_reversion import RSI2MeanReversion

    STRATEGY_REGISTRY["rsi2"] = RSI2MeanReversion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tb", description="tradingBot CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print account snapshot + open positions")
    sub.add_parser("halt", help="Touch HALT file to stop a running loop")
    sub.add_parser("reconcile", help="Reconcile local DB state against broker (Phase 3)")

    bt = sub.add_parser("backtest", help="Run a backtest")
    bt.add_argument("strategy", choices=["rsi2"], help="Strategy name")
    bt.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD inclusive")
    bt.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD inclusive")
    bt.add_argument("--symbols", required=True, help="Comma-separated, e.g. SPY,QQQ")
    bt.add_argument("--equity", type=float, default=100_000.0)
    bt.add_argument("--slippage-bps", type=float, default=5.0)

    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings)

    if args.cmd == "status":
        return _cmd_status(settings)
    if args.cmd == "halt":
        return _cmd_halt()
    if args.cmd == "reconcile":
        print("reconcile: not implemented yet (Phase 3)")
        return 1
    if args.cmd == "backtest":
        return _cmd_backtest(settings, args)
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


if __name__ == "__main__":
    sys.exit(main())
