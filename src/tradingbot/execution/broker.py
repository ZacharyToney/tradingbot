from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from alpaca.data.enums import DataFeed
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoLatestQuoteRequest,
    StockLatestQuoteRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from loguru import logger

from tradingbot.config import Settings


def canon_symbol(s: str) -> str:
    """Normalize a symbol for cross-source comparison.

    Alpaca returns crypto as "SOLUSD" but the bot tracks "SOL/USD" everywhere else
    (strategy universes, signals, fills, intended orders). Strip "/" so both shapes
    map to the same key.
    """
    return s.replace("/", "")


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    daytrade_count: int


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    broker_order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float | None


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts_ms: int


class AlpacaBroker:
    """Thin wrapper. Always uses paper unless settings.trading_mode == 'live'."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret,
            paper=settings.is_paper,
        )
        # Market-data clients reused for latest-quote snapshots on submit.
        self._stock_data = StockHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_secret
        )
        self._crypto_data = CryptoHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_secret
        )

    @property
    def is_paper(self) -> bool:
        return self._settings.is_paper

    def get_account(self) -> AccountSnapshot:
        a = self._client.get_account()
        return AccountSnapshot(
            equity=float(a.equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            daytrade_count=int(a.daytrade_count or 0),
        )

    def get_positions(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
            )
            for p in self._client.get_all_positions()
        ]

    def submit_market_order(
        self,
        symbol: str,
        side: str,           # "buy" | "sell"
        qty: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> OrderResult:
        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif_map = {"day": TimeInForce.DAY, "gtc": TimeInForce.GTC, "ioc": TimeInForce.IOC}
        tif = tif_map[time_in_force]
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=tif,
            client_order_id=client_order_id,
        )
        logger.info(
            f"submit market_order symbol={symbol} side={side} qty={qty} cid={client_order_id}"
        )
        try:
            o = self._client.submit_order(req)
        except Exception as e:
            if _is_duplicate_cid_error(e):
                logger.warning(f"duplicate client_order_id, skipped cid={client_order_id}")
                existing = self._client.get_order_by_client_id(client_order_id)
                return _to_result(existing)
            raise
        return _to_result(o)

    def cancel_all(self) -> int:
        results = self._client.cancel_orders()
        n = len(results) if results else 0
        logger.info(f"cancel_all count={n}")
        return n

    def get_order_by_client_id(self, cid: str) -> OrderResult | None:
        try:
            o = self._client.get_order_by_client_id(cid)
        except Exception:
            return None
        return _to_result(o)

    def get_latest_quote(self, symbol: str) -> Quote | None:
        """Best-effort latest top-of-book quote. Returns None on any failure so callers
        never block trading on a measurement-only data path."""
        try:
            if "/" in symbol:
                req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                resp = self._crypto_data.get_crypto_latest_quote(req)
            else:
                req = StockLatestQuoteRequest(
                    symbol_or_symbols=symbol,
                    feed=DataFeed(self._settings.equity_data_feed),
                )
                resp = self._stock_data.get_stock_latest_quote(req)
            q = resp.get(symbol)
            if q is None:
                return None
            return Quote(
                bid=float(q.bid_price),
                ask=float(q.ask_price),
                bid_size=float(q.bid_size or 0),
                ask_size=float(q.ask_size or 0),
                ts_ms=int(q.timestamp.timestamp() * 1000),
            )
        except Exception as e:
            logger.warning(f"get_latest_quote failed symbol={symbol}: {e}")
            return None


def _is_duplicate_cid_error(e: Exception) -> bool:
    """Alpaca returns: code 40010001 or message containing 'client_order_id must be unique'."""
    msg = str(e).lower()
    if "client_order_id" in msg and ("unique" in msg or "already exists" in msg):
        return True
    code = getattr(e, "code", None) or getattr(e, "_code", None)
    return code == 40010001


def _to_result(o) -> OrderResult:
    return OrderResult(
        client_order_id=str(o.client_order_id),
        broker_order_id=str(o.id),
        status=str(o.status.value if isinstance(o.status, OrderStatus) else o.status),
        filled_qty=float(o.filled_qty or 0),
        filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
    )


def open_order_statuses() -> Iterable[str]:
    return ("new", "accepted", "pending_new", "partially_filled")
