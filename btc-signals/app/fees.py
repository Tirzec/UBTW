"""Polymarket fee curve and order-book walking.

Docs (docs.polymarket.com/trading/fees): fee = C * feeRate * p * (1 - p), takers only.
Gamma exposes it per market as feeSchedule = {rate, exponent, takerOnly}; we
apply fee = C * rate * (p * (1 - p)) ** exponent so a schedule change in the
API is picked up automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SCHEDULE = {"rate": 0.07, "exponent": 1, "takerOnly": True}


def fee_per_share(price: float, schedule: dict | None = None) -> float:
    s = schedule or DEFAULT_SCHEDULE
    p = min(max(price, 0.0), 1.0)
    return float(s.get("rate", 0.07)) * (p * (1 - p)) ** float(s.get("exponent", 1))


def bankers_cents(x: float) -> float:
    """Round to the nearest $0.01, half to even (Polymarket US: 'banker's rounding')."""
    from decimal import ROUND_HALF_EVEN, Decimal
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def fee_usdc(shares: float, price: float, schedule: dict | None = None) -> float:
    """Fee for ONE fill. Polymarket US rounds each fill's fee to the cent with banker's
    rounding (docs.polymarket.us/fees), signalled by schedule["round_cents"]."""
    raw = shares * fee_per_share(price, schedule)
    return bankers_cents(raw) if (schedule or {}).get("round_cents") else raw


@dataclass
class Fill:
    shares: float
    avg_price: float      # excluding fees
    cost: float           # USDC paid (buy) or received (sell), excluding fees
    fee: float
    worst_price: float
    complete: bool        # False if the book was too thin

    @property
    def avg_price_with_fee(self) -> float:
        if not self.shares:
            return 0.0
        return (self.cost + self.fee) / self.shares


def walk_buy(asks: list[tuple[float, float]], usd: float, schedule: dict | None = None) -> Fill:
    """Buy up to `usd` of shares against asks sorted best (lowest) first.

    Fees are charged on top of `usd` per filled level."""
    remaining, shares, cost, fee, worst = usd, 0.0, 0.0, 0.0, 0.0
    for price, size in asks:
        if remaining <= 1e-9:
            break
        take = min(size, remaining / price)
        shares += take
        cost += take * price
        fee += fee_usdc(take, price, schedule)
        remaining -= take * price
        worst = price
    avg = cost / shares if shares else 0.0
    return Fill(shares, avg, cost, fee, worst, remaining <= 1e-6)


def walk_sell(bids: list[tuple[float, float]], shares: float, schedule: dict | None = None) -> Fill:
    """Sell `shares` against bids sorted best (highest) first."""
    remaining, filled, proceeds, fee, worst = shares, 0.0, 0.0, 0.0, 0.0
    for price, size in bids:
        if remaining <= 1e-9:
            break
        take = min(size, remaining)
        filled += take
        proceeds += take * price
        fee += fee_usdc(take, price, schedule)
        remaining -= take
        worst = price
    avg = proceeds / filled if filled else 0.0
    return Fill(filled, avg, proceeds, fee, worst, remaining <= 1e-6)
