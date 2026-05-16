from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from tradingbot.data.bars import TF


@runtime_checkable
class Strategy(Protocol):
    """Pure-function strategy: given a symbol's bar history, return target weights.

    The strategy must be a deterministic function of `df[:t]`. The backtest
    engine slices the DataFrame before each call to enforce no look-ahead, but
    strategy authors must not stash future data inside `__init__` either.
    """

    name: str
    universe: list[str]
    timeframe: TF

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> pd.Series:
        """Return target weights in [-1, 1], indexed by bar timestamp.

        +1 = full long, 0 = flat, -1 = full short. The backtest engine (and
        live runner) scale by `max_position_pct`. NaNs are treated as 0.
        """
        ...
