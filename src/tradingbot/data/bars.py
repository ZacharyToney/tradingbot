from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger

from tradingbot.config import Settings

AssetClass = Literal["equity", "crypto"]


@dataclass(frozen=True)
class TF:
    """Lightweight timeframe spec we own. Maps to alpaca-py TimeFrame on the way out."""
    amount: int
    unit: Literal["Min", "Hour", "Day"]

    @property
    def key(self) -> str:
        return f"{self.amount}{self.unit}"

    def to_alpaca(self) -> TimeFrame:
        if self.unit == "Min":
            return TimeFrame(self.amount, TimeFrameUnit.Minute)
        if self.unit == "Hour":
            return TimeFrame(self.amount, TimeFrameUnit.Hour)
        if self.unit == "Day":
            return TimeFrame(self.amount, TimeFrameUnit.Day)
        raise ValueError(f"unknown unit {self.unit}")


def _classify(symbol: str) -> AssetClass:
    return "crypto" if "/" in symbol else "equity"


class BarSource:
    """Pulls historical OHLCV bars from Alpaca. No caching here — caller layer handles that."""

    def __init__(self, settings: Settings):
        self._stock = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret)
        self._crypto = CryptoHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret)
        self._stock_feed = DataFeed(settings.equity_data_feed)

    def get_bars(
        self,
        symbol: str,
        timeframe: TF,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        asset_class = _classify(symbol)
        tf = timeframe.to_alpaca()
        logger.debug(f"fetch bars symbol={symbol} tf={timeframe.key} start={start} end={end}")
        if asset_class == "equity":
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                end=end,
                feed=self._stock_feed,
            )
            resp = self._stock.get_stock_bars(req)
        else:
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            resp = self._crypto.get_crypto_bars(req)
        df = resp.df
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0)
        return df[["open", "high", "low", "close", "volume"]].copy()
