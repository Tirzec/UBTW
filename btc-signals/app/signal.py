"""Entry decision: compare model probability with what it costs to buy each side."""
from __future__ import annotations

from dataclasses import dataclass, field

from .fees import walk_buy

Book = dict  # {"bids": [(p, s), ...] best first, "asks": [(p, s), ...] best first}


@dataclass
class SideQuote:
    side: str
    p_model: float
    best_ask: float | None
    best_bid: float | None
    avg_fill: float | None       # stake-weighted ask after walking the book
    fee_per_share: float
    edge: float | None           # p_model - avg_fill - fee - buffer
    raw_edge: float | None       # p_model - best_ask (before costs)
    depth_ok: bool

    def as_dict(self) -> dict:
        r = lambda x: None if x is None else round(x, 4)  # noqa: E731
        return {"side": self.side, "p_model": r(self.p_model), "best_ask": self.best_ask,
                "best_bid": self.best_bid, "avg_fill": r(self.avg_fill),
                "fee_per_share": r(self.fee_per_share), "edge": r(self.edge),
                "raw_edge": r(self.raw_edge), "depth_ok": self.depth_ok}


@dataclass
class Decision:
    action: str                          # "UP" | "DOWN" | "NO TRADE"
    edge: float | None
    quotes: dict[str, SideQuote]
    blocked_by: list[str] = field(default_factory=list)
    best_side: str | None = None         # side with the higher edge even if blocked

    def as_dict(self) -> dict:
        return {"action": self.action, "edge": None if self.edge is None else round(self.edge, 4),
                "best_side": self.best_side, "blocked_by": self.blocked_by,
                "quotes": {k: v.as_dict() for k, v in self.quotes.items()}}


def quote_side(side: str, p_model: float, book: Book | None, stake_usd: float, buffer: float,
               fee_schedule: dict | None) -> SideQuote:
    asks = (book or {}).get("asks") or []
    bids = (book or {}).get("bids") or []
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    if not asks:
        return SideQuote(side, p_model, None, best_bid, None, 0.0, None, None, False)
    fill = walk_buy(asks, stake_usd, fee_schedule)
    fee_ps = fill.fee / fill.shares if fill.shares else 0.0
    edge = p_model - fill.avg_price - fee_ps - buffer
    return SideQuote(side, p_model, best_ask, best_bid, fill.avg_price, fee_ps, edge,
                     p_model - best_ask, fill.complete)


def decide(p_up: float, books: dict[str, Book | None], *, buffer: float, stake_usd: float,
           max_spread: float, fee_schedule: dict | None, gates: list[str]) -> Decision:
    """Signal a side only if its edge after fees, spread/slippage and buffer is > 0
    and no gate (rules, data quality, calendar, news, timing...) blocks it."""
    quotes = {
        "UP": quote_side("UP", p_up, books.get("UP"), stake_usd, buffer, fee_schedule),
        "DOWN": quote_side("DOWN", 1.0 - p_up, books.get("DOWN"), stake_usd, buffer, fee_schedule),
    }
    blocked = list(gates)
    cands = [q for q in quotes.values() if q.edge is not None]
    best = max(cands, key=lambda q: q.edge) if cands else None
    if best is None:
        blocked.append("no order book")
        return Decision("NO TRADE", None, quotes, blocked, None)
    if best.best_bid is not None and best.best_ask is not None \
            and best.best_ask - best.best_bid > max_spread + 1e-9:
        blocked.append(f"spread {best.best_ask - best.best_bid:.2f} > {max_spread:.2f}")
    if not best.depth_ok:
        blocked.append(f"book too thin for ${stake_usd:g}")
    if best.edge <= 0:
        blocked.append(f"edge {best.edge:+.3f} below 0 after fees + buffer")
    action = best.side if not blocked else "NO TRADE"
    return Decision(action, best.edge, quotes, blocked, best.side)
