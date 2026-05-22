"""Track B v0.1: aggressive 1h Donchian breakout on crypto.

Entry (long): 1h close > rolling max of close over prior `enter_window` bars (12h default).
Exit:         1h close < rolling min of close over prior `exit_window` bars (6h default),
              OR `time_stop_bars` (24h default) elapsed since entry — kills any position
              that's been sitting around without resolving.

This is the chaos sandbox. No edge claim. Faster timeframe + looser risk caps than the
daily Donchian on Track A. Lives on a separate Alpaca paper account so it cannot
contaminate Track A's evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tradingbot.data.bars import TF


def compute_indicators(
    df: pd.DataFrame, enter_window: int = 12, exit_window: int = 6
) -> pd.DataFrame:
    """Same shape as donchian_breakout.compute_indicators but tuned for 1h bars.

    hi[t] = max(close[t-enter_window:t])  — STRICTLY prior bars, no current
    lo[t] = min(close[t-exit_window:t])
    """
    close = df["close"].astype(float)
    hi = close.shift(1).rolling(enter_window).max()
    lo = close.shift(1).rolling(exit_window).min()
    return pd.DataFrame({"hi": hi, "lo": lo}, index=df.index)


def signals_from_indicators(
    close: pd.Series,
    hi: pd.Series,
    lo: pd.Series,
    time_stop_bars: int = 24,
) -> pd.Series:
    """State machine: enter on breakout, exit on low OR time stop.

    The time stop kills positions that have been sitting in-position for > N bars
    without either triggering the low exit or making a new high. Prevents the bot
    from indefinitely holding through chop.
    """
    n = len(close)
    out = np.zeros(n, dtype=float)
    in_pos = False
    bars_since_entry = 0
    for i in range(n):
        h = hi.iloc[i] if i < len(hi) else np.nan
        l = lo.iloc[i] if i < len(lo) else np.nan  # noqa: E741
        c = close.iloc[i]
        if in_pos:
            bars_since_entry += 1
            low_exit = not pd.isna(l) and c < l
            time_exit = bars_since_entry >= time_stop_bars
            if low_exit or time_exit:
                in_pos = False
                bars_since_entry = 0
                out[i] = 0.0
            else:
                out[i] = 1.0
        else:
            if not pd.isna(h) and c > h:
                in_pos = True
                bars_since_entry = 0
                out[i] = 1.0
            else:
                out[i] = 0.0
    return pd.Series(out, index=close.index, name="target_weight")


@dataclass(frozen=True)
class ChaosCryptoMomentum:
    name: str = "chaos"
    enter_window: int = 12
    exit_window: int = 6
    time_stop_bars: int = 24
    timeframe: TF = TF(1, "Hour")

    @property
    def universe(self) -> list[str]:
        return ["BTC/USD", "ETH/USD", "SOL/USD"]

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        ind = compute_indicators(
            df, enter_window=self.enter_window, exit_window=self.exit_window
        )
        return signals_from_indicators(
            close=df["close"].astype(float),
            hi=ind["hi"],
            lo=ind["lo"],
            time_stop_bars=self.time_stop_bars,
        )
