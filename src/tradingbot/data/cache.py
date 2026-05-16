"""Simple parquet cache for historical bars.

v1 policy: "all-or-refetch-all". If the cached parquet covers the requested
range, slice it. Otherwise fetch the full range via BarSource, overwrite the
cache, return. Gap-filling is Phase 3 polish.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from tradingbot.data.bars import TF, BarSource


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _asset_class(symbol: str) -> str:
    return "crypto" if "/" in symbol else "equity"


def _cache_path(cache_dir: Path, symbol: str, tf: TF) -> Path:
    return cache_dir / _asset_class(symbol) / _safe_symbol(symbol) / f"{tf.key}.parquet"


def load_bars(
    source: BarSource,
    symbol: str,
    tf: TF,
    start: datetime,
    end: datetime,
    cache_dir: Path,
) -> pd.DataFrame:
    path = _cache_path(cache_dir, symbol, tf)

    if path.exists():
        try:
            cached = pd.read_parquet(path)
            cached.index = pd.to_datetime(cached.index, utc=True)
            covered = (
                len(cached) > 0
                and cached.index.min() <= pd.Timestamp(start, tz="UTC")
                and cached.index.max() >= pd.Timestamp(end, tz="UTC") - pd.Timedelta(days=4)
            )
            if covered:
                return cached.loc[pd.Timestamp(start, tz="UTC"): pd.Timestamp(end, tz="UTC")]
        except Exception:
            pass  # corrupt cache → refetch

    fresh = source.get_bars(symbol, tf, start=start, end=end)
    if fresh.empty:
        return fresh
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh.index = pd.to_datetime(fresh.index, utc=True)
    fresh.to_parquet(path)
    return fresh
