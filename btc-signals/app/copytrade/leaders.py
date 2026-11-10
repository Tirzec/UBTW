"""Watch the approved traders' fills on international Polymarket (public Data API v2, read-only).

Polls /v2/trades?user=<wallet> every few seconds (with a changing `_` parameter: the CDN caches 300 s).
The first poll only records what already happened (nothing old is ever copied). New fills are grouped per
(token, side) within one poll into a LeaderTrade. For a SELL the trader's remaining holding is read from
/v2/positions so the copier can sell the same FRACTION.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("copytrade")
DATA = "https://data-api.polymarket.com/v2"


@dataclass
class LeaderTrade:
    wallet: str
    name: str
    side: str                       # BUY | SELL
    token_id: str
    condition_id: str
    slug: str
    event_slug: str
    title: str
    outcome: str
    outcome_index: int
    size: float                     # shares in this batch
    price: float                    # VWAP of this batch (price of the token they traded)
    first_ts: float                 # exchange time of the first fill
    seen_ts: float                  # when we saw it
    fills: int = 1
    sell_fraction: Optional[float] = None   # SELL only: share of their holding they sold
    holding_after: Optional[float] = None
    raw: list = field(default_factory=list)

    @property
    def lag_s(self) -> float:
        return self.seen_ts - self.first_ts


def fill_key(r: dict) -> tuple:
    return (r.get("transaction_hash"), str(r.get("token_id")), r.get("side"), float(r.get("size") or 0),
            float(r.get("price") or 0), r.get("timestamp"))


def group_new(rows: list[dict], wallet: str, name: str, now: float) -> list[LeaderTrade]:
    """New fills (any order) -> one LeaderTrade per (token, side), oldest first (pure, unit-tested)."""
    g: dict[tuple, LeaderTrade] = {}
    for r in sorted(rows, key=lambda r: r.get("timestamp") or 0):
        try:
            size, px = float(r.get("size") or 0), float(r.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or r.get("side") not in ("BUY", "SELL"):
            continue
        k = (str(r.get("token_id")), r["side"])
        t = g.get(k)
        if t is None:
            g[k] = LeaderTrade(wallet=wallet, name=name, side=r["side"], token_id=str(r.get("token_id")),
                               condition_id=r.get("condition_id") or "", slug=r.get("slug") or "",
                               event_slug=r.get("event_slug") or "", title=r.get("title") or "",
                               outcome=r.get("outcome") or "", outcome_index=int(r.get("outcome_index") or 0),
                               size=size, price=px, first_ts=float(r.get("timestamp") or now), seen_ts=now,
                               raw=[r])
        else:
            t.price = (t.price * t.size + px * size) / (t.size + size)
            t.size += size
            t.fills += 1
            t.raw.append(r)
    return sorted(g.values(), key=lambda t: t.first_ts)


class LeaderWatcher:
    def __init__(self, client: httpx.AsyncClient, traders: list[dict], interval_s: float = 3.0,
                 max_pages: int = 5):
        self.client = client
        self.traders = {t["wallet"].lower(): t for t in traders}
        self.interval_s = interval_s
        self.max_pages = max_pages
        self.seen: dict[str, set] = {}
        self.ready: dict[str, bool] = {}
        self.last_poll: dict[str, float] = {}
        self.last_fill_ts: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.lags: list[float] = []

    async def _get(self, path: str, params: dict) -> dict:
        p = dict(params)
        p["_"] = time.time_ns()
        r = await self.client.get(f"{DATA}/{path}", params=p)
        r.raise_for_status()
        return r.json()

    async def poll(self, wallet: str) -> list[LeaderTrade]:
        name = self.traders[wallet].get("name") or wallet[:10]
        seen = self.seen.setdefault(wallet, set())
        new, cursor = [], None
        for _ in range(self.max_pages):
            p = {"user": wallet, "limit": 100, "taker_only": "false"}
            if cursor:
                p["cursor"] = cursor
            d = await self._get("trades", p)
            rows = d.get("data") or []
            fresh = [r for r in rows if fill_key(r) not in seen]
            new += fresh
            pg = d.get("pagination") or {}
            cursor = pg.get("next_cursor")
            if not self.ready.get(wallet) or len(fresh) < len(rows) or not pg.get("has_more") or not cursor:
                break
        for r in new:
            seen.add(fill_key(r))
        now = time.time()
        self.last_poll[wallet] = now
        if new:
            self.last_fill_ts[wallet] = max(float(r.get("timestamp") or 0) for r in new)
        if not self.ready.get(wallet):          # baseline: never copy what happened before we started
            self.ready[wallet] = True
            if self.last_fill_ts.get(wallet) is None and new:
                self.last_fill_ts[wallet] = max(float(r.get("timestamp") or 0) for r in new)
            return []
        out = group_new(new, wallet, name, now)
        for t in out:
            self.lags.append(t.lag_s)
            if t.side == "SELL":
                await self._sell_fraction(t)
        self.lags = self.lags[-200:]
        return out

    async def _sell_fraction(self, t: LeaderTrade) -> None:
        """Fraction of their holding this sell was: sold / (sold + what they still hold)."""
        last_fill = max(float(r.get("timestamp") or 0) for r in t.raw) if t.raw else t.first_ts
        try:
            for attempt in range(3):
                d = await self._get("positions", {"user": t.wallet, "condition": t.condition_id, "limit": 50})
                rows = [r for r in d.get("data") or [] if str(r.get("token_id")) == t.token_id]
                left = sum(float(r.get("current_size") or 0) for r in rows)
                fresh = not rows or max(float(r.get("last_event_at") or 0) for r in rows) >= last_fill
                if fresh:
                    break
                await asyncio.sleep(1.0)
            if not fresh:                       # positions not updated for this sale yet: it still counts the
                left = max(0.0, left - t.size)  # shares just sold, so take them off
            t.holding_after = left
            t.sell_fraction = min(1.0, t.size / (t.size + left)) if t.size + left > 0 else 1.0
        except (httpx.HTTPError, ValueError) as e:
            log.warning("positions for %s: %s", t.name, e)
            t.sell_fraction = None              # unknown: the copier keeps holding (never dumps blindly)

    async def run(self, on_trade) -> None:
        """Poll every trader forever; await on_trade(LeaderTrade) for each new trade."""
        while True:
            for w in list(self.traders):
                try:
                    for t in await self.poll(w):
                        await on_trade(t)
                    self.errors.pop(w, None)
                except Exception as e:  # noqa: BLE001 - keep watching every trader whatever happens
                    self.errors[w] = f"{type(e).__name__}: {e}"[:200]
                    log.warning("leader poll %s: %s", w[:10], e)
            await asyncio.sleep(self.interval_s)
