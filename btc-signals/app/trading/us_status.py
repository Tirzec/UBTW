"""Polymarket US market status and official settlement from the public gateway (read-only, no keys).

Used where no strategy module owns a market: the reconciler (is a market the exchange no longer lists simply
closed/settled?) and the settlement of leftover LIVE positions of a module that is switched off.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from ..ingest.us_markets import GATEWAY, nocache

OPEN = "MARKET_STATUS_OPEN"
RESOLVED = "MARKET_STATUS_RESOLVED"


class UsMarketStatus:
    def __init__(self, client: httpx.AsyncClient, cache_s: float = 20.0):
        self.client = client
        self.cache_s = cache_s
        self._status: dict[str, tuple[float, Optional[str]]] = {}
        self.settled: dict[str, float] = {}

    async def _get(self, path: str) -> Optional[dict]:
        url = httpx.URL(GATEWAY + path).copy_merge_params(nocache())   # keeps the path's own query
        for i in range(3):
            r = await self.client.get(url)
            if r.status_code == 404:
                return None
            if (r.status_code == 429 or r.status_code >= 500) and i < 2:
                await asyncio.sleep(2.0 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        return None

    async def status(self, slug: str, fresh: bool = False) -> Optional[str]:
        """MARKET_STATUS_* of a market (None: unknown market / not found)."""
        hit = self._status.get(slug)
        if hit and not fresh and time.time() - hit[0] < self.cache_s:
            return hit[1]
        d = await self._get(f"/v1/market/slug/{slug}")
        st = ((d or {}).get("market") or {}).get("status")
        self._status[slug] = (time.time(), st)
        return st

    async def closed(self, slug: str) -> bool:
        """True when the market no longer trades (closed, settling or resolved) - its positions are settling."""
        st = await self.status(slug, fresh=True)
        return bool(st) and st != OPEN

    async def settlement(self, slug: str) -> Optional[float]:
        """Official settlement price of the LONG side (1.0 / 0.0, or a 'last fair price'), once resolved."""
        if slug in self.settled:
            return self.settled[slug]
        if await self.status(slug) != RESOLVED:
            return None
        s = await self._get(f"/v1/markets/{slug}/settlement")
        if not s or s.get("settlement") is None:
            return None
        self.settled[slug] = float(s["settlement"])
        return self.settled[slug]
