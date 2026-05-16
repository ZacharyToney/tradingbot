"""Phase 1 smoke test.

Verifies end-to-end Alpaca paper connectivity:
  1. Load settings from .env.
  2. Pull recent SPY daily bars (read path).
  3. Get account snapshot.
  4. Submit a 1-share SPY market order with a deterministic client_order_id
     (idempotent: rerunning the same UTC-day call must NOT create a duplicate).
  5. Poll briefly for fill, print result.
  6. Cancel any open orders we left around.

Run with:
    uv run python scripts/smoke_paper_order.py
"""
from __future__ import annotations

import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger

from tradingbot.config import load_settings
from tradingbot.data.bars import TF, BarSource
from tradingbot.execution.broker import AlpacaBroker
from tradingbot.logging_setup import configure_logging


def _deterministic_cid(label: str) -> str:
    """Idempotent within a UTC day — rerunning won't duplicate the order."""
    day = datetime.now(UTC).strftime("%Y%m%d")
    namespace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    return f"smoke-{label}-{uuid.uuid5(namespace, day)}"


def main() -> int:
    settings = load_settings()
    configure_logging(settings)
    logger.info(f"smoke start mode={settings.trading_mode}")

    if not settings.is_paper:
        logger.error("refusing to run smoke against LIVE; set TRADING_MODE=paper")
        return 2

    # 1) Bars
    bars = BarSource(settings)
    end = datetime.now(UTC)
    start = end - timedelta(days=200)
    df = bars.get_bars("SPY", TF(1, "Day"), start=start, end=end)
    logger.info(f"bars rows={len(df)} cols={list(df.columns)}")
    if df.empty:
        logger.error("got zero bars — check API keys and entitlements")
        return 3
    print(df.tail(3))

    # 2) Account
    broker = AlpacaBroker(settings)
    acc = broker.get_account()
    logger.info(f"account equity={acc.equity:.2f} cash={acc.cash:.2f} "
                f"buying_power={acc.buying_power:.2f} daytrades={acc.daytrade_count}")

    # 3) Order — 1 share SPY market. Idempotent within a UTC day.
    cid = _deterministic_cid("spy-1-share")
    result = broker.submit_market_order(symbol="SPY", side="buy", qty=1, client_order_id=cid)
    logger.info(f"order submitted cid={result.client_order_id} status={result.status}")

    # 4) Poll for terminal status (filled / cancelled / rejected) for up to 10s
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = broker.get_order_by_client_id(cid)
        if latest is None:
            time.sleep(0.5)
            continue
        if latest.status in {"filled", "cancelled", "rejected"}:
            result = latest
            break
        time.sleep(0.5)

    logger.info(f"final status={result.status} filled_qty={result.filled_qty} "
                f"avg_price={result.filled_avg_price}")

    # 5) Cleanup
    broker.cancel_all()
    logger.info("smoke done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
