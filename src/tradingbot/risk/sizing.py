from __future__ import annotations

from tradingbot.risk.limits import AssetClass

CRYPTO_DUST_USD = 1.0
EQUITY_DUST_SHARES = 1


def size_position(
    target_weight: float,
    equity_for_sizing: float,
    price: float,
    max_position_pct: float,
    asset_class: AssetClass,
) -> float:
    """Fixed-fractional sizing. Signed return: positive=long, negative=short, 0=flat or dust.

    Equities: whole shares (truncated toward 0).
    Crypto:   fractional (no rounding beyond float precision).
    Dust:     equity qty < 1 share OR crypto notional < $1 → 0.
    """
    if equity_for_sizing <= 0 or price <= 0:
        return 0.0

    # Clip weight to ±1 and apply cap.
    clipped = max(-1.0, min(1.0, target_weight))
    if clipped == 0:
        return 0.0
    notional = abs(clipped) * max_position_pct * equity_for_sizing
    raw_qty = notional / price

    if asset_class == "equity":
        whole = int(raw_qty)  # truncate toward 0
        if whole < EQUITY_DUST_SHARES:
            return 0.0
        return float(whole) if clipped > 0 else -float(whole)

    # Crypto
    if notional < CRYPTO_DUST_USD:
        return 0.0
    return raw_qty if clipped > 0 else -raw_qty
