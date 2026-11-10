"""Pick who to copy (step 2): score international top traders, exclude the uncopyable, backtest
"copy with a delay" (10-30 s later at the price a copier could then get, US taker fees) and keep only traders
still profitable after the delay.

Pure functions take plain dicts (unit-tested); `IntlData` does the I/O.
Bias controls (review 2026-11-10): traders come from the CURRENT monthly leaderboard, so they are scored and
backtested on the EARLIER part of the window (out of sample); the recent month is shown separately.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

# 5/15-minute crypto up/down markets (btc-updown-15m-<ts>, bitcoin-up-or-down-...-15-min-...)
FAST_CRYPTO = re.compile(
    r"updown-(5|15)m\b"
    r"|^(btc|eth|sol|xrp|doge|bnb|hype|bitcoin|ethereum|solana|dogecoin)-.*(up-?or-?down|updown).*\b(5|15)-?m(in(ute)?s?)?\b",
    re.I)


# ---------------------------------------------------------------- fills -> entries
def group_fills(fills: list[dict], gap_s: int = 120, max_span_s: int = 300) -> list[dict]:
    """A trader's 'trade' is usually many fills within seconds. Merge consecutive fills of the same token
    and side (each <= gap_s after the previous, whole run <= max_span_s) into one entry with the volume-
    weighted price. A fill on the other side of the same token ends the run. Oldest first."""
    rows = sorted((f for f in fills if f.get("timestamp") is not None), key=lambda f: (f["timestamp"], f.get("side", "")))
    out: list[dict] = []
    open_: dict[tuple, dict] = {}
    for f in rows:
        tok, side = str(f.get("token_id")), f.get("side")
        size, px = float(f.get("size") or 0), float(f.get("price") or 0)
        if size <= 0:
            continue
        for k in [k for k in open_ if k[0] == tok and k[1] != side]:
            del open_[k]                               # opposite side on this token: the run is over
        key = (tok, side)
        e = open_.get(key)
        if e is not None and f["timestamp"] - e["t_last"] <= gap_s and f["timestamp"] - e["t"] <= max_span_s:
            e["notional"] += size * px
            e["size"] += size
            e["t_last"] = f["timestamp"]
            e["fills"] += 1
            continue
        e = {"token": tok, "condition": f.get("condition_id"), "side": side,
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


def position_pnl(c: dict) -> float:
    """Realized P&L of a finished position. Resolved-but-unredeemed rows (status OPEN, redeemable) are worth
    their current value (0 for a loser)."""
    if c.get("status") == "OPEN":
        cost = float(c.get("avg_price") or 0) * float(c.get("total_size") or 0)
        return float(c.get("current_value") or 0) - cost + float(c.get("realized_pnl") or 0)
    return float(c.get("realized_pnl") or 0)


def score_trader(*, wallet: str, name: str, fills: list[dict], truncated: bool, closed: list[dict],
                 pnl_points: list[dict], since: float, now: float, min_trades: int = 50,
                 max_entries_per_day: float = 40, max_fills_per_day: float = 400) -> dict:
    """Stats over [since, now] and the exclusion reasons (empty list = eligible). `closed` = finished
    positions: CLOSED rows plus resolved-unredeemed OPEN rows (otherwise losers are missing)."""
    days = max(1.0, (now - since) / 86400)
    win = [f for f in fills if since <= (f.get("timestamp") or 0) <= now]
    entries = group_fills(win)
    buys = [e for e in entries if e["side"] == "BUY"]
    ts = [f["timestamp"] for f in win]
    span = max(1.0, (max(ts) - min(ts)) / 86400) if ts else days          # active span inside the window
    fills_per_day = len(win) / span
    entries_per_day = len(buys) / span
    # market making: buys on both outcomes of a market, or flipping the same token back and forth
    by_cond: dict[str, dict] = {}
    for e in entries:
        d = by_cond.setdefault(e["condition"], {})
        d.setdefault(e["token"], {"BUY": 0, "SELL": 0})[e["side"]] += 1
    traded = [d for d in by_cond.values() if any(v["BUY"] for v in d.values())]
    two = sum(1 for d in traded if sum(1 for v in d.values() if v["BUY"]) > 1
              or any(v["BUY"] >= 3 and v["SELL"] >= 3 for v in d.values()))
    both = two / len(traded) if traded else 0.0
    fast = sum(1 for e in buys if FAST_CRYPTO.search(e["slug"] or "")) / len(buys) if buys else 0.0
    sweep = sum(1 for e in buys if e["price"] >= 0.97) / len(buys) if buys else 0.0
    # finished positions in the window: P&L, ROI, win rate, concentration
    cl = [c for c in closed if since <= (c.get("last_event_at") or 0) <= now]
    pnls = [position_pnl(c) for c in cl]
    cost = sum(float(c.get("avg_price") or 0) * float(c.get("total_size") or 0) for c in cl)
    wins = [p for p in pnls if p > 0]
    realized = sum(pnls)
    top2 = sum(sorted(wins, reverse=True)[:2])
    # daily P&L curve: drawdown and weekly consistency
    pts = sorted((p for p in pnl_points if since - 86400 <= (p.get("timestamp") or 0) <= now), key=lambda p: p["timestamp"])
    curve = [float(p.get("economic_pnl") if p.get("economic_pnl") is not None else p.get("realized_pnl") or 0)
             for p in pts]
    curve_pnl = (curve[-1] - curve[0]) if len(curve) >= 2 else None
    weeks = [curve[i + 7] - curve[i] for i in range(0, len(curve) - 7, 7)]
    s = {"wallet": wallet, "name": name, "days": round(days), "entries": len(buys), "fills": len(win),
         "fills_per_day": round(fills_per_day, 1), "entries_per_day": round(entries_per_day, 1),
         "both_sides_share": round(both, 3), "fast_crypto_share": round(fast, 3), "sweep_share": round(sweep, 3),
         "positions": len(cl), "realized_pnl": round(realized, 2), "cost": round(cost, 2),
         "roi": (realized / cost) if cost > 0 else None, "win_rate": (len(wins) / len(cl)) if cl else None,
         "top2_share": (top2 / realized) if realized > 0 else None, "ex_top2_pnl": round(realized - top2, 2),
         "curve_pnl": curve_pnl, "max_drawdown": max_drawdown(curve) if curve else None,
         "profitable_weeks": (sum(1 for w in weeks if w > 0) / len(weeks)) if weeks else None,
         "truncated": truncated}
    why = []
    if entries_per_day > max_entries_per_day or fills_per_day > max_fills_per_day:
        why.append(f"high-frequency ({entries_per_day:.0f} trades / {fills_per_day:.0f} fills a day: too fast to copy)")
    if both > 0.25:
        why.append(f"market maker: trades both sides in {both:.0%} of markets")
    if fast > 0.4:
        why.append(f"{fast:.0%} of trades in 5/15-minute crypto (too fast to copy)")
    if sweep > 0.4:
        why.append(f"{sweep:.0%} of buys at 97c+ (buys already-decided outcomes: nothing left to copy)")
    if len(buys) < min_trades:
        why.append(f"only {len(buys)} trades (< {min_trades})")
    if realized <= 0 or (curve_pnl is not None and curve_pnl <= 0):
        why.append("not profitable in the window")
    elif top2 / realized > 0.5 or realized - top2 <= 0:
        why.append(f"{top2 / realized:.0%} of profit from 1-2 bets (P&L without them {realized - top2:+,.0f})")
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
    open_pnl: float = 0.0
    open_n: int = 0
    gaps: list = field(default_factory=list)          # my price - their price on copied buys
    by_mkt: dict = field(default_factory=dict)        # condition -> P&L (for a lower bound)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def add(self, cond, pnl: float) -> None:
        self.pnl += pnl
        self.by_mkt[cond] = self.by_mkt.get(cond, 0.0) + pnl

    def as_dict(self) -> dict:
        v = list(self.by_mkt.values())
        n = len(v)
        lb = (sum(v) - statistics.stdev(v) * n ** 0.5) if n >= 2 else None     # P&L minus 1 standard error
        return {"delay_s": self.delay_s, "copied": self.copied, "skipped": self.skipped,
                "cost": round(self.cost, 2), "fees": round(self.fees, 2), "pnl": round(self.pnl, 2),
                "realized_pnl": round(self.pnl - self.open_pnl, 2), "open_pnl": round(self.open_pnl, 2),
                "roi": (self.pnl / self.cost) if self.cost else None,
                "win_rate": (self.wins / self.closed) if self.closed else None, "closed": self.closed,
                "open": self.open_n, "markets": n, "pnl_lb": None if lb is None else round(lb, 2),
                "avg_copy_cost": statistics.mean(self.gaps) if self.gaps else None}


def taker_fee(shares: float, price: float, rate: float) -> float:
    return rate * shares * price * (1 - price)


def backtest_copy(entries: list[dict], *, price_after, market_info, delays=(10, 30), stake: float = 1.0,
                  max_worse: float = 0.03, slip: float = 0.01, fee_rate: float = 0.095,
                  min_time_to_end_s: int = 3600, copy_from: float = 0.0, copy_to: float = math.inf,
                  wallet: str | None = None, now: Optional[float] = None) -> dict[int, dict]:
    """Replay a trader's entries as a copier who acts `delay` seconds later.

    price_after(condition, token, t, side, wallet) -> price a copier could BUY (ask side) / SELL (bid side)
        at/after t, from other people's taker fills, or None
    market_info(condition, token) -> {"end_ts", "payout" (resolved), "mark" (current price)}
    The trader's holdings are tracked over ALL entries given; only entries in [copy_from, copy_to) are copied.
    BUY: copy $stake if the copy price is <= their price + max_worse (3c) and the market resolves > 1 h later.
    SELL/reduce: sell the same fraction of my position (skipped if they sell more than we saw them buy:
    the fraction is unknown, so keep holding rather than dump everything).
    Not sold: paid at resolution, or valued at the current price minus slippage and the exit fee.
    slip = extra cost per share on top of the observed price (international -> US price gap, impact).
    """
    now = now or time.time()
    out = {}
    for d in delays:
        r = CopyResult(d)
        mine: dict[str, list] = {}                      # token -> [shares, cost basis, condition]
        theirs: dict[str, float] = {}                   # their shares seen so far
        realized: dict[str, float] = {}                 # token -> realized P&L of my current position
        for e in entries:
            tok = e["token"]
            if e["side"] == "BUY":
                theirs[tok] = theirs.get(tok, 0.0) + e["size"]
                if not copy_from <= e["t"] < copy_to:
                    continue
                info = market_info(e["condition"], tok) or {}
                end = info.get("end_ts")
                if end is not None and end - e["t"] < min_time_to_end_s:
                    r.skip("market resolves within 1 h")
                    continue
                px = price_after(e["condition"], tok, e["t"] + d, "BUY", wallet)
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
                m = mine.setdefault(tok, [0.0, 0.0, e["condition"]])
                m[0] += sh
                m[1] += stake + fee
                r.copied += 1
                r.cost += stake
                r.fees += fee
                r.gaps.append(buy - e["price"])
            else:                                       # SELL: reduce by the same fraction
                held = theirs.get(tok, 0.0)
                theirs[tok] = max(0.0, held - e["size"])
                m = mine.get(tok)
                if not m or m[0] <= 1e-9 or not copy_from <= e["t"] < copy_to:
                    continue
                if held <= 1e-9 or e["size"] > held * 1.02:
                    r.skip("sell larger than the position we saw (fraction unknown, kept holding)")
                    continue
                px = price_after(e["condition"], tok, e["t"] + d, "SELL", wallet)
                if px is None:
                    r.skip("exit not priced (kept holding)")
                    continue
                frac = min(1.0, e["size"] / held)
                sell = max(0.01, px - slip)
                sh, basis = m[0] * frac, m[1] * frac
                fee = taker_fee(sh, sell, fee_rate)
                r.fees += fee
                pnl = sh * sell - fee - basis
                m[0] -= sh
                m[1] -= basis
                r.add(m[2], pnl)
                realized[tok] = realized.get(tok, 0.0) + pnl
                if m[0] <= 1e-6:
                    r.closed += 1
                    r.wins += int(realized.pop(tok, 0.0) > 0)
                    del mine[tok]
        for tok, (sh, basis, cond) in mine.items():
            info = market_info(cond, tok) or {}
            prior = realized.pop(tok, 0.0)
            if info.get("payout") is not None:
                pnl = sh * info["payout"] - basis
                r.add(cond, pnl)
                r.closed += 1
                r.wins += int(pnl + prior > 0)
            elif info.get("mark") is not None:
                exit_px = max(0.01, info["mark"] - slip)
                fee = taker_fee(sh, exit_px, fee_rate)
                r.fees += fee
                val = sh * exit_px - fee
                r.add(cond, val - basis)
                r.open_pnl += val - basis
                r.open_value += val
                r.open_n += 1
            else:
                r.add(cond, -basis)                     # unknown: assume worthless (conservative)
                r.open_pnl -= basis
                r.skip("unpriced open position (counted as $0)")
        out[d] = r.as_dict()
    return out
