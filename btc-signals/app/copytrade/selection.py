"""Pick who to copy (step 2): score international top traders, exclude the uncopyable, backtest
"copy with a delay" (10-30 s later at the then-current price, US taker fees) and keep only traders still
profitable after the delay.

Pure functions take plain dicts (unit-tested); `IntlData` does the I/O.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

FAST_CRYPTO = re.compile(r"(updown-(5|15)m|-(5|15)m-|up-or-down-(5|15)|(5|15)-?min)", re.I)


# ---------------------------------------------------------------- fills -> entries
def group_fills(fills: list[dict], gap_s: int = 120) -> list[dict]:
    """A trader's 'trade' is usually many fills within seconds. Merge consecutive fills of the same token
    and side (no more than gap_s apart) into one entry with the volume-weighted price. Oldest first."""
    rows = sorted((f for f in fills if f.get("timestamp") is not None), key=lambda f: (f["timestamp"], f.get("side", "")))
    out: list[dict] = []
    open_: dict[tuple, dict] = {}
    for f in rows:
        key = (str(f.get("token_id")), f.get("side"))
        e = open_.get(key)
        size, px = float(f.get("size") or 0), float(f.get("price") or 0)
        if size <= 0:
            continue
        if e is not None and f["timestamp"] - e["t_last"] <= gap_s:
            e["notional"] += size * px
            e["size"] += size
            e["t_last"] = f["timestamp"]
            e["fills"] += 1
            continue
        e = {"token": str(f.get("token_id")), "condition": f.get("condition_id"), "side": f.get("side"),
             "t": float(f["timestamp"]), "t_last": float(f["timestamp"]), "size": size, "notional": size * px,
             "slug": f.get("slug") or "", "event_slug": f.get("event_slug") or "", "title": f.get("title") or "",
             "outcome": f.get("outcome"), "outcome_index": f.get("outcome_index"), "fills": 1}
        open_[key] = e
        out.append(e)
    for e in out:
        e["price"] = e["notional"] / e["size"]
    return sorted(out, key=lambda e: e["t"])


# ---------------------------------------------------------------- scoring
def max_drawdown(series: list[float]) -> float:
    peak, dd = -math.inf, 0.0
    for v in series:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def score_trader(*, wallet: str, name: str, fills: list[dict], truncated: bool, closed: list[dict],
                 pnl_points: list[dict], since: float, now: float, min_trades: int = 50,
                 max_entries_per_day: float = 40) -> dict:
    """Stats over [since, now] and the exclusion reasons (empty list = eligible)."""
    days = max(1.0, (now - since) / 86400)
    entries = group_fills([f for f in fills if (f.get("timestamp") or 0) >= since])
    buys = [e for e in entries if e["side"] == "BUY"]
    # market making / high frequency: both outcomes bought in the same market, or very many fills
    by_cond: dict[str, set] = {}
    for e in buys:
        by_cond.setdefault(e["condition"], set()).add(e["token"])
    both = sum(1 for toks in by_cond.values() if len(toks) > 1) / len(by_cond) if by_cond else 0.0
    ts = [f["timestamp"] for f in fills if f.get("timestamp")]
    span = max(1.0, (max(ts) - min(ts)) / 86400) if truncated and ts else days   # truncated: covered span only
    fills_per_day = len(fills) / span
    entries_per_day = len(buys) / span
    fast = sum(1 for e in buys if FAST_CRYPTO.search(e["slug"] or "")) / len(buys) if buys else 0.0
    sweep = sum(1 for e in buys if e["price"] >= 0.97) / len(buys) if buys else 0.0
    # closed positions in the window: P&L, ROI, win rate, concentration
    cl = [c for c in closed if (c.get("last_event_at") or 0) >= since]
    pnls = [float(c.get("realized_pnl") or 0) for c in cl]
    cost = sum(float(c.get("avg_price") or 0) * float(c.get("total_size") or 0) for c in cl)
    wins = [p for p in pnls if p > 0]
    pos_total = sum(wins)
    top2 = sum(sorted(wins, reverse=True)[:2])
    realized = sum(pnls)
    # daily P&L curve: drawdown and weekly consistency
    pts = sorted((p for p in pnl_points if (p.get("timestamp") or 0) >= since - 86400), key=lambda p: p["timestamp"])
    curve = [float(p.get("economic_pnl") if p.get("economic_pnl") is not None else p.get("realized_pnl") or 0)
             for p in pts]
    curve_pnl = (curve[-1] - curve[0]) if len(curve) >= 2 else None
    weeks = [curve[i + 7] - curve[i] for i in range(0, len(curve) - 7, 7)]
    s = {"wallet": wallet, "name": name, "days": round(days), "entries": len(buys), "fills": len(fills),
         "fills_per_day": round(fills_per_day, 1), "entries_per_day": round(entries_per_day, 1), "both_sides_share": round(both, 3), "fast_crypto_share": round(fast, 3),
         "sweep_share": round(sweep, 3),
         "closed_positions": len(cl), "realized_pnl": round(realized, 2), "cost": round(cost, 2),
         "roi": (realized / cost) if cost > 0 else None, "win_rate": (len(wins) / len(cl)) if cl else None,
         "top2_share": (top2 / pos_total) if pos_total > 0 else None, "curve_pnl": curve_pnl,
         "max_drawdown": max_drawdown(curve) if curve else None,
         "profitable_weeks": (sum(1 for w in weeks if w > 0) / len(weeks)) if weeks else None}
    why = []
    if entries_per_day > max_entries_per_day:
        why.append(f"high-frequency ({entries_per_day:.0f} trades a day: too fast to copy)")
    if both > 0.25:
        why.append(f"market maker: bought both sides in {both:.0%} of markets")
    if fast > 0.4:
        why.append(f"{fast:.0%} of trades in 5/15-minute crypto (too fast to copy)")
    if sweep > 0.4:
        why.append(f"{sweep:.0%} of buys at 97c+ (buys already-decided outcomes: nothing left to copy)")
    if len(buys) < min_trades:
        why.append(f"only {len(buys)} trades (< {min_trades})")
    if realized <= 0 and (curve_pnl is None or curve_pnl <= 0):
        why.append("not profitable in the window")
    if s["top2_share"] is not None and s["top2_share"] > 0.5 and realized > 0:
        why.append(f"{s['top2_share']:.0%} of profit from 1-2 bets")
    s["excluded"] = why
    s["_entries"] = entries
    return s


# ---------------------------------------------------------------- delayed-copy backtest
@dataclass
class CopyResult:
    delay_s: int
    copied: int = 0
    skipped: dict = field(default_factory=dict)
    cost: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0
    wins: int = 0
    closed: int = 0
    open_value: float = 0.0
    open_n: int = 0
    gaps: list = field(default_factory=list)          # my price - their price on copied buys

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def as_dict(self) -> dict:
        return {"delay_s": self.delay_s, "copied": self.copied, "skipped": self.skipped,
                "cost": round(self.cost, 2), "fees": round(self.fees, 2), "pnl": round(self.pnl, 2),
                "roi": (self.pnl / self.cost) if self.cost else None,
                "win_rate": (self.wins / self.closed) if self.closed else None, "closed": self.closed,
                "open": self.open_n, "avg_copy_cost": statistics.mean(self.gaps) if self.gaps else None}


def taker_fee(shares: float, price: float, rate: float) -> float:
    return rate * shares * price * (1 - price)


def backtest_copy(entries: list[dict], *, price_after, market_info, delays=(10, 30), stake: float = 1.0,
                  max_worse: float = 0.03, slip: float = 0.005, fee_rate: float = 0.095,
                  min_time_to_end_s: int = 3600, now: Optional[float] = None) -> dict[int, dict]:
    """Replay a trader's entries as a copier who acts `delay` seconds later.

    price_after(condition, token, t) -> price of `token` first traded at/after t, or None
    market_info(condition, token) -> {"end_ts": float|None, "payout": float|None (resolved), "mark": float|None}
    BUY: copy $stake if the delayed price is <= their price + max_worse (3c) and the market ends > 1 h later.
    SELL/reduce: sell the same fraction of my position at the delayed price.
    Not sold: paid at resolution, or marked to the current price if still open.
    """
    now = now or time.time()
    out = {}
    for d in delays:
        r = CopyResult(d)
        mine: dict[str, list] = {}                      # token -> [shares, cost basis]
        theirs: dict[str, float] = {}                   # their shares seen in the window
        for e in entries:
            tok = e["token"]
            if e["side"] == "BUY":
                theirs[tok] = theirs.get(tok, 0.0) + e["size"]
                info = market_info(e["condition"], tok) or {}
                end = info.get("end_ts")
                if end is not None and end - e["t"] < min_time_to_end_s:
                    r.skip("market ends within 1 h")
                    continue
                px = price_after(e["condition"], tok, e["t"] + d)
                if px is None:
                    r.skip("no trade to price the copy")
                    continue
                if px > e["price"] + max_worse + 1e-9:
                    r.skip("price more than 3c worse")
                    continue
                buy = px + slip
                if buy > 0.99:
                    r.skip("price above 99c")
                    continue
                sh = stake / buy
                fee = taker_fee(sh, buy, fee_rate)
                m = mine.setdefault(tok, [0.0, 0.0])
                m[0] += sh
                m[1] += stake + fee
                r.copied += 1
                r.cost += stake
                r.fees += fee
                r.gaps.append(buy - e["price"])
            else:                                       # SELL: reduce by the same fraction
                held_theirs = theirs.get(tok, 0.0)
                m = mine.get(tok)
                if not m or m[0] <= 1e-9 or held_theirs <= 1e-9:
                    theirs[tok] = max(0.0, held_theirs - e["size"])
                    continue
                frac = min(1.0, e["size"] / held_theirs)
                theirs[tok] = max(0.0, held_theirs - e["size"])
                px = price_after(e["condition"], tok, e["t"] + d)
                if px is None:
                    continue                            # can't price the exit: keep holding
                sell = max(0.01, px - slip)
                sh = m[0] * frac
                basis = m[1] * frac
                fee = taker_fee(sh, sell, fee_rate)
                r.fees += fee
                pnl = sh * sell - fee - basis
                m[0] -= sh
                m[1] -= basis
                r.pnl += pnl
                if m[0] <= 1e-9:
                    r.closed += 1
                    r.wins += int(pnl > 0)              # approximation for partial exits
        for tok, (sh, basis) in mine.items():
            if sh <= 1e-9:
                continue
            cond = next((e["condition"] for e in entries if e["token"] == tok), None)
            info = market_info(cond, tok) or {}
            if info.get("payout") is not None:
                pnl = sh * info["payout"] - basis
                r.pnl += pnl
                r.closed += 1
                r.wins += int(pnl > 0)
            elif info.get("mark") is not None:
                r.pnl += sh * info["mark"] - basis
                r.open_value += sh * info["mark"]
                r.open_n += 1
            else:
                r.pnl -= basis                          # unknown: assume worthless (conservative)
                r.skip("unpriced open position (counted as $0)")
        out[d] = r.as_dict()
    return out
