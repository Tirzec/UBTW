"""Read-only client for the international Polymarket public data (signal source for copy trading).

Nothing here places orders: orders go ONLY to Polymarket US (user's rule). The user accepted on 2026-11-10
that reading this data from the US conflicts with polymarket.com's terms of service (option 1).

- Data API v2  https://data-api.polymarket.com/v2  (cursor paging; CDN caches 300 s -> every request gets a
  changing `_` parameter so we never read minutes-old trades)
- Gamma        https://gamma-api.polymarket.com     (market metadata, resolutions, events[].usId)
Rate limits (docs, per IP): /v2 800 req/10 s, /v2/trades 300 req/10 s. We pace far below that.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator, Optional

import httpx

log = logging.getLogger("copytrade")
DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"


class IntlData:
    def __init__(self, client: Optional[httpx.Client] = None, min_gap_s: float = 0.05, timeout: float = 20):
        self.c = client or httpx.Client(timeout=timeout, headers={"User-Agent": "btc-signals/1.0"})
        self.min_gap = min_gap_s                     # <= 20 req/s in total (all threads), under the documented limits
        self._last = 0.0
        self._pace = threading.Lock()                # shared by worker threads: the pacing is global
        self._market_cache: dict[str, Optional[dict]] = {}
        self._event_cache: dict[str, Optional[dict]] = {}
        self.calls = 0

    # ------------------------------------------------------------ transport
    def _get(self, url: str, params: dict | None = None, tries: int = 4) -> Any:
        params = dict(params or {})
        params["_"] = time.time_ns()                 # bust the 300 s CDN cache
        for i in range(tries):
            with self._pace:
                wait = self.min_gap - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
                self.calls += 1
            try:
                r = self.c.get(url, params=params)
            except httpx.HTTPError as e:
                if i == tries - 1:
                    raise
                log.warning("GET %s failed (%s), retrying", url, e)
                time.sleep(1.5 * (i + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("retry-after") or 0) or 2.0 * (i + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{url} -> HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise RuntimeError(f"{url}: gave up after {tries} tries")

    def _pages(self, path: str, params: dict, max_pages: int = 50) -> Iterator[dict]:
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            d = self._get(f"{DATA}/{path}", p)
            for row in d.get("data") or []:
                yield row
            pg = d.get("pagination") or {}
            cursor = pg.get("next_cursor")
            if not pg.get("has_more") or not cursor:
                return

    # ------------------------------------------------------------ data api
    def leaderboard(self, period: str = "month", sort_by: str = "PNL", category: str | None = None,
                    n: int = 200) -> list[dict]:
        p = {"time_period": period, "sort_by": sort_by, "limit": min(n, 1000)}
        if category:
            p["category"] = category
        out = []
        for row in self._pages("leaderboard", p, max_pages=max(1, n // 1000 + 1)):
            out.append(row)
            if len(out) >= n:
                break
        return out

    def user_stats(self, wallet: str) -> dict:
        return self._get(f"{DATA}/user-stats", {"user": wallet}).get("data") or {}

    def user_pnl(self, wallet: str, interval: str = "all", fidelity: str = "1d") -> list[dict]:
        d = self._get(f"{DATA}/user-pnl", {"user": wallet, "interval": interval, "fidelity": fidelity})
        return (d.get("data") or {}).get("points") or []

    def user_trades(self, wallet: str, start: int | None = None, end: int | None = None,
                    max_rows: int = 20000) -> tuple[list[dict], bool]:
        """Every fill (maker AND taker: taker_only=false) newest first. Returns (rows, truncated)."""
        p: dict = {"user": wallet, "limit": 1000, "taker_only": "false"}
        if start:
            p["start"] = int(start)
        if end:
            p["end"] = int(end)
        out = []
        for row in self._pages("trades", p, max_pages=max_rows // 1000 + 1):
            out.append(row)
            if len(out) >= max_rows:
                return out, True
        return out, False

    def positions(self, wallet: str, status: str, start: int | None = None, max_rows: int = 5000,
                  sort_by: str | None = None) -> list[dict]:
        p: dict = {"user": wallet, "status": status, "limit": 500}
        if start:
            p["start"] = int(start)
        if sort_by:
            p["sort_by"] = sort_by
        out = []
        for row in self._pages("positions", p, max_pages=max_rows // 500 + 1):
            out.append(row)
            if len(out) >= max_rows:
                break
        return out

    def closed_positions(self, wallet: str, start: int | None = None, max_rows: int = 5000) -> list[dict]:
        """Closed positions moved inside the window, newest first (sorted by time, NOT by P&L: the default
        P&L order would keep only the biggest winners when capped)."""
        return self.positions(wallet, "CLOSED", start, max_rows, sort_by="TIMESTAMP")

    def resolved_open_positions(self, wallet: str, start: int | None = None, max_rows: int = 5000) -> list[dict]:
        """Resolved but never redeemed (losers usually sit here at price 0): needed for honest win rates."""
        rows = self.positions(wallet, "OPEN", start, max_rows, sort_by="TIMESTAMP")
        return [r for r in rows if r.get("redeemable")]

    def market_trades(self, condition_id: str, start: int, end: int, limit: int = 1000,
                      taker_only: bool = True) -> list[dict]:
        """Fills in one market between start and end (newest first, one page). taker_only=True: each fill
        once, from the taker's side (side = the aggressor's side on that token)."""
        d = self._get(f"{DATA}/trades", {"condition": condition_id, "start": int(start), "end": int(end),
                                         "limit": limit, "taker_only": "true" if taker_only else "false"})
        return d.get("data") or []

    def price_after(self, condition_id: str, token_id: str, t: float, side: str = "BUY",
                    exclude_wallet: str | None = None, window_s: int = 900,
                    slice_s: int = 5) -> Optional[tuple[float, float]]:
        """The price a copier could trade `token_id` at, at/after time t: from taker fills in the copier's
        direction only (BUY -> someone lifting the ask; SELL -> someone hitting the bid). A taker trade on the
        other outcome counts as the complement (each condition has exactly two tokens). The copied trader's
        own fills are ignored. Worst print within the first `slice_s` seconds. Returns (timestamp, price)."""
        return pick_price(lambda w: self.market_trades(condition_id, int(t), int(t) + w), token_id, t, side,
                          exclude_wallet, windows=(15, 120, window_s), slice_s=slice_s)

    # ------------------------------------------------------------ gamma
    def market(self, condition_id: str) -> Optional[dict]:
        if condition_id not in self._market_cache:
            d = self._get(f"{GAMMA}/markets", {"condition_ids": condition_id, "limit": 1})
            if not d:
                d = self._get(f"{GAMMA}/markets", {"condition_ids": condition_id, "closed": "true", "limit": 1})
            self._market_cache[condition_id] = d[0] if d else None
        return self._market_cache[condition_id]

    def event(self, slug: str) -> Optional[dict]:
        if slug not in self._event_cache:
            d = self._get(f"{GAMMA}/events", {"slug": slug})
            self._event_cache[slug] = d[0] if d else None
        return self._event_cache[slug]


def resolution_payout(market: Optional[dict], token_id: str) -> Optional[float]:
    """$ per share the token paid at resolution (1, 0, or 0.5 for 50-50), None if not resolved."""
    import json
    if not market or not market.get("closed"):
        return None
    if (market.get("umaResolutionStatus") or "resolved") not in ("resolved",):
        return None
    try:
        toks = json.loads(market.get("clobTokenIds") or "[]")
        prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return None
    if str(token_id) not in toks or len(prices) != len(toks):
        return None
    p = prices[toks.index(str(token_id))]
    return p if p in (0.0, 0.5, 1.0) else None


def pick_price(fetch, token_id: str, t: float, side: str, exclude_wallet: str | None = None,
               windows=(15, 120, 900), slice_s: int = 5) -> Optional[tuple[float, float]]:
    """Pure part of price_after (unit-tested): fetch(window_s) -> taker fills in [t, t + window_s]."""
    ex = (exclude_wallet or "").lower()
    cand: list[tuple[float, float]] = []
    for w in windows:                               # narrowest non-empty window wins (pages are newest first)
        cand = []
        for r in fetch(w):
            ts = r.get("timestamp")
            if ts is None or ts < t or (ex and (r.get("proxy_wallet") or "").lower() == ex):
                continue
            p = float(r["price"])
            if str(r.get("token_id")) == str(token_id):
                s_, px = r.get("side"), p
            else:
                s_, px = ("SELL" if r.get("side") == "BUY" else "BUY"), 1.0 - p
            if s_ == side:
                cand.append((float(ts), px))
        if cand:
            break
    if not cand:
        return None
    t0 = min(ts for ts, _ in cand)
    first = [px for ts, px in cand if ts <= t0 + slice_s]
    return t0, (max(first) if side == "BUY" else min(first))
