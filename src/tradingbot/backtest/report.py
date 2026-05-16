from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from tradingbot.backtest.engine import BacktestResult
from tradingbot.backtest.metrics import Metrics


def write_report(
    result: BacktestResult,
    metrics: Metrics,
    out_root: Path,
    extra: dict[str, str] | None = None,
) -> Path:
    """Write summary.txt + trades.csv + equity.csv under {out_root}/{strategy}_{utc_ts}/."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_root / f"{result.strategy_name or 'strategy'}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Summary
    lines: list[str] = []
    lines.append(f"strategy: {result.strategy_name}")
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append(f"trade_count:     {metrics.trade_count}")
    lines.append(f"win_rate:        {metrics.win_rate:.4f}")
    lines.append(f"profit_factor:   {metrics.profit_factor:.4f}")
    lines.append(f"sharpe (ann.):   {metrics.sharpe:.4f}")
    lines.append(f"sortino (ann.):  {metrics.sortino:.4f}")
    lines.append(f"max_drawdown:    {metrics.max_dd:.4f}")
    lines.append(f"cagr:            {metrics.cagr:.4f}")
    if len(result.equity_curve) >= 2:
        lines.append("")
        lines.append(f"starting_equity: {result.equity_curve.iloc[0]:.2f}")
        lines.append(f"ending_equity:   {result.equity_curve.iloc[-1]:.2f}")
        total_return = result.equity_curve.iloc[-1] / result.equity_curve.iloc[0] - 1.0
        lines.append(f"total_return:    {total_return:.4f}")
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")

    # Trades
    with (out_dir / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["symbol", "side", "entry_ts", "exit_ts", "entry_price", "exit_price", "qty", "pnl"]
        )
        for t in result.trades:
            w.writerow(
                [
                    t.symbol,
                    t.side,
                    t.entry_ts.isoformat(),
                    t.exit_ts.isoformat(),
                    f"{t.entry_price:.6f}",
                    f"{t.exit_price:.6f}",
                    f"{t.qty:.6f}",
                    f"{t.pnl:.6f}",
                ]
            )

    # Equity curve
    result.equity_curve.to_csv(out_dir / "equity.csv", header=["equity"])

    return out_dir
