"""Donchian channel breakout on crypto.

Entry (long): close[t] > rolling max of close over [t-enter_window, t-1] (i.e. prior
              `enter_window` bars, EXCLUDING t itself — no look-ahead).
Exit:         close[t] < rolling min of close over [t-exit_window, t-1].
Side:         long-only for v1. (Shorts on downside breakout add cost-of-carry concerns
              and crypto trends are structurally long-biased.)
Universe:     BTC/USD, ETH/USD, SOL/USD. 24/7 market — no PDT, no gap risk.

No trend filter (unlike RSI(2) on equities). Donchian's edge in crypto comes from
trend-persistence; an SMA filter would gut the late-breakout entries.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tradingbot.data.bars import TF


def compute_indicators(
    df: pd.DataFrame, enter_window: int = 20, exit_window: int = 10
) -> pd.DataFrame:
    """Return DataFrame with [hi, lo] indexed to match df.

    hi[t] = max(close[t-enter_window:t])   — STRICTLY prior bars, no current
    lo[t] = min(close[t-exit_window:t])
    """
    close = df["close"].astype(float)
    # shift(1) drops today's value; rolling().max() then takes the prior `window` bars.
    hi = close.shift(1).rolling(enter_window).max()
    lo = close.shift(1).rolling(exit_window).min()
    return pd.DataFrame({"hi": hi, "lo": lo}, index=df.index)


def signals_from_indicators(
    close: pd.Series,
    hi: pd.Series,
    lo: pd.Series,
) -> pd.Series:
    """State machine: enter when close > hi, exit when close < lo."""
    n = len(close)
    out = np.zeros(n, dtype=float)
    in_pos = False
    for i in range(n):
        h = hi.iloc[i] if i < len(hi) else np.nan
        l = lo.iloc[i] if i < len(lo) else np.nan  # noqa: E741
        c = close.iloc[i]
        if in_pos:
            if not pd.isna(l) and c < l:
                in_pos = False
                out[i] = 0.0
            else:
                out[i] = 1.0
        else:
            if not pd.isna(h) and c > h:
                in_pos = True
                out[i] = 1.0
            else:
                out[i] = 0.0
    return pd.Series(out, index=close.index, name="target_weight")


@dataclass(frozen=True)
class DonchianBreakout:
    name: str = "donchian"
    enter_window: int = 20
    exit_window: int = 10
    timeframe: TF = TF(1, "Day")

    @property
    def universe(self) -> list[str]:
        return ["BTC/USD", "ETH/USD", "SOL/USD"]

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        ind = compute_indicators(df, enter_window=self.enter_window, exit_window=self.exit_window)
        return signals_from_indicators(
            close=df["close"].astype(float),
            hi=ind["hi"],
            lo=ind["lo"],
        )
