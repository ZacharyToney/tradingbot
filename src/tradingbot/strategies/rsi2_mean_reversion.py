"""RSI(2) Connors mean reversion.

Entry:  RSI(2) < rsi_buy_threshold AND close > SMA(trend_filter_window)
Hold:   while in position, target_weight stays at +1.0
Exit:   RSI(2) > rsi_sell_threshold OR max_hold_bars elapsed since entry
Side:   long only. Trend filter is the protection — no hard stop.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tradingbot.data.bars import TF


def compute_indicators(df: pd.DataFrame, sma_window: int = 200) -> pd.DataFrame:
    """Return a DataFrame with [rsi2, sma] indexed to match df."""
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    # Wilder smoothing via EMA with alpha = 1/period. Period=2.
    avg_gain = gain.ewm(alpha=0.5, adjust=False).mean()
    avg_loss = loss.ewm(alpha=0.5, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi2 = 100 - (100 / (1 + rs))
    rsi2 = rsi2.where(avg_loss != 0, other=100.0)  # all-up sequence → RSI = 100
    rsi2 = rsi2.where(avg_gain + avg_loss > 0, other=np.nan)  # warmup → NaN
    sma = close.rolling(sma_window).mean()
    return pd.DataFrame({"rsi2": rsi2, "sma": sma}, index=df.index)


def signals_from_indicators(
    close: pd.Series,
    rsi2: pd.Series,
    sma: pd.Series,
    rsi_buy_threshold: float = 10.0,
    rsi_sell_threshold: float = 70.0,
    max_hold_bars: int = 5,
) -> pd.Series:
    """Walk bar-by-bar producing target_weight ∈ {0, 1}.

    State machine:
      flat -> in_pos on entry condition
      in_pos -> flat on exit condition (RSI > sell OR held >= max_hold_bars)
    Signal at bar t reflects the position AFTER processing bar t's information.
    """
    n = len(close)
    out = np.zeros(n, dtype=float)
    in_pos = False
    bars_held = 0
    for i in range(n):
        r = rsi2.iloc[i] if i < len(rsi2) else np.nan
        s = sma.iloc[i] if i < len(sma) else np.nan
        c = close.iloc[i]
        if in_pos:
            bars_held += 1
            exit_rsi = not pd.isna(r) and r > rsi_sell_threshold
            exit_time = bars_held >= max_hold_bars
            if exit_rsi or exit_time:
                in_pos = False
                bars_held = 0
                out[i] = 0.0
            else:
                out[i] = 1.0
        else:
            entry = (
                not pd.isna(r)
                and not pd.isna(s)
                and r < rsi_buy_threshold
                and c > s
            )
            if entry:
                in_pos = True
                bars_held = 0
                out[i] = 1.0
            else:
                out[i] = 0.0
    return pd.Series(out, index=close.index, name="target_weight")


@dataclass(frozen=True)
class RSI2MeanReversion:
    name: str = "rsi2"
    trend_filter_window: int = 200
    rsi_buy_threshold: float = 10.0
    rsi_sell_threshold: float = 70.0
    max_hold_bars: int = 5
    timeframe: TF = TF(1, "Day")

    @property
    def universe(self) -> list[str]:
        return ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        ind = compute_indicators(df, sma_window=self.trend_filter_window)
        return signals_from_indicators(
            close=df["close"].astype(float),
            rsi2=ind["rsi2"],
            sma=ind["sma"],
            rsi_buy_threshold=self.rsi_buy_threshold,
            rsi_sell_threshold=self.rsi_sell_threshold,
            max_hold_bars=self.max_hold_bars,
        )
