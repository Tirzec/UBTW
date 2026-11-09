"""Perp derivatives context: funding, open interest, book imbalance, liquidations.

Default provider is Hyperliquid (public, no key). Binance futures is optional
(geo-blocked in the US). Liquidations are best-effort: Hyperliquid's public
info API has no liquidation feed, so `liq_imbalance` stays 0 and is flagged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

from ..features import book_imbalance

log = logging.getLogger("derivs")


class Derivatives:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        dc = cfg.get("derivatives", {})
        self.enabled = dc.get("enabled", True)
        self.provider = dc.get("provider", "hyperliquid")
        self.interval = float(dc.get("poll_interval_s", 10))
        self.hl_url = dc.get("hyperliquid_url", "https://api.hyperliquid.xyz/info")
        self.bn_url = dc.get("binance_url", "https://fapi.binance.com")
        self.client = client or httpx.AsyncClient(timeout=10)
        self.oi_hist: deque[tuple[float, float]] = deque(maxlen=200)
        self.state: dict = {"provider": self.provider, "ok": False, "liquidations_available": False}

    def snapshot(self) -> dict:
        s = dict(self.state)
        if s.get("updated") and time.time() - s["updated"] > 4 * self.interval:
            s["ok"] = False
            s["error"] = "stale"
        return s

    def features(self) -> dict:
        s = self.snapshot()
        if not s.get("ok"):
            return {}
        return {"funding_bps_hr": s.get("funding_bps_hr", 0.0),
                "oi_change_pct_5m": s.get("oi_change_pct_5m", 0.0),
                "liq_imbalance": s.get("liq_imbalance", 0.0),
                "book_imb": s.get("book_imb", 0.0)}

    def _oi_change(self, now: float, oi: float) -> float:
        self.oi_hist.append((now, oi))
        past = [v for t, v in self.oi_hist if t <= now - 300]
        base = past[-1] if past else self.oi_hist[0][1]
        return (oi - base) / base * 100 if base else 0.0

    async def poll_hyperliquid(self) -> None:
        r = await self.client.post(self.hl_url, json={"type": "metaAndAssetCtxs"})
        r.raise_for_status()
        meta, ctxs = r.json()
        i = [a["name"] for a in meta["universe"]].index("BTC")
        c = ctxs[i]
        b = await self.client.post(self.hl_url, json={"type": "l2Book", "coin": "BTC"})
        b.raise_for_status()
        lv = b.json()["levels"]
        bids = [(float(x["px"]), float(x["sz"])) for x in lv[0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in lv[1]]
        now = time.time()
        oi = float(c["openInterest"])
        self.state = {
            "provider": "hyperliquid", "ok": True, "updated": now,
            "funding_bps_hr": float(c["funding"]) * 1e4,     # hourly funding rate
            "premium_bps": float(c.get("premium") or 0) * 1e4,
            "open_interest": oi, "oi_change_pct_5m": self._oi_change(now, oi),
            "mark": float(c["markPx"]), "oracle": float(c["oraclePx"]),
            "book_imb": book_imbalance(bids, asks, 10),
            "liq_imbalance": 0.0, "liquidations_available": False,
        }

    async def poll_binance(self) -> None:
        g = self.client.get
        p, o, d = await asyncio.gather(
            g(f"{self.bn_url}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}),
            g(f"{self.bn_url}/fapi/v1/openInterest", params={"symbol": "BTCUSDT"}),
            g(f"{self.bn_url}/fapi/v1/depth", params={"symbol": "BTCUSDT", "limit": 20}))
        for r in (p, o, d):
            r.raise_for_status()
        now = time.time()
        oi = float(o.json()["openInterest"])
        dj = d.json()
        bids = [(float(a), float(b)) for a, b in dj["bids"]]
        asks = [(float(a), float(b)) for a, b in dj["asks"]]
        self.state = {
            "provider": "binance", "ok": True, "updated": now,
            "funding_bps_hr": float(p.json()["lastFundingRate"]) * 1e4 / 8,  # 8h rate -> hourly
            "open_interest": oi, "oi_change_pct_5m": self._oi_change(now, oi),
            "mark": float(p.json()["markPrice"]), "book_imb": book_imbalance(bids, asks, 10),
            "liq_imbalance": 0.0, "liquidations_available": False,
        }

    async def run(self) -> None:
        if not self.enabled:
            self.state = {"provider": self.provider, "ok": False, "error": "disabled"}
            return
        poll = self.poll_binance if self.provider == "binance" else self.poll_hyperliquid
        while True:
            try:
                await poll()
            except Exception as e:  # noqa: BLE001
                self.state = {**self.state, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
                log.warning("derivatives poll failed: %s", self.state["error"])
            await asyncio.sleep(self.interval)
