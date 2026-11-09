"""FeedManager: runs every configured feed, picks the healthy one per role,
builds 1s series, and produces the reference (Chainlink) price with fallback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..series import SecondBars, SecondValues
from .base import PriceFeed, PriceTick
from .quality import DataQualityMonitor, QualityState
from .sources import build_feed

log = logging.getLogger("feeds")


@dataclass
class ReferencePrice:
    spot: float | None        # Chainlink BTC/USD (or synthetic estimate)
    twap: float | None        # Chainlink 60s TWAP (or synthetic estimate)
    source: str               # feed name, or "synthetic:<spot feed>"
    synthetic: bool
    ts: float


class FeedManager:
    def __init__(self, cfg: dict):
        fc = cfg.get("feeds", {})
        health = fc.get("health", {})
        secrets = cfg.get("secrets", {})
        self.reference_feeds: list[PriceFeed] = [
            build_feed(c, "reference", secrets, health) for c in fc.get("reference", [])]
        self.spot_feeds: list[PriceFeed] = [
            build_feed(c, "spot", secrets, health) for c in fc.get("spot", [])]
        names = [f.name for f in self.reference_feeds + self.spot_feeds]
        if len(set(names)) != len(names):
            raise ValueError("feed names must be unique; set `name:` when using a type twice")
        self.quality = DataQualityMonitor(cfg.get("data_quality", {}))
        self.bars: dict[str, SecondBars] = {f.name: SecondBars() for f in self.spot_feeds}
        self.ref_spot: dict[str, SecondValues] = {f.name: SecondValues() for f in self.reference_feeds}
        self.ref_twap: dict[str, SecondValues] = {f.name: SecondValues() for f in self.reference_feeds}
        self.basis_bps = 0.0          # EWMA of chainlink vs spot, used for the synthetic estimate
        self._active_ref: str | None = None
        self._active_spot: str | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ ingest
    def on_tick(self, t: PriceTick) -> None:
        if t.kind == "trade":
            self.bars[t.source].add_trade(t.ts, t.price, t.size, t.side)
        elif t.kind == "spot":
            self.ref_spot[t.source].add(t.ts, t.price)
        elif t.kind == "twap":
            self.ref_twap[t.source].add(t.ts, t.price)

    async def start(self) -> None:
        for f in self.reference_feeds + self.spot_feeds:
            self._tasks.append(asyncio.create_task(f.run(self.on_tick), name=f"feed:{f.name}"))
        self._tasks.append(asyncio.create_task(self._health_loop(), name="feed-health"))

    async def stop(self) -> None:
        for f in self.reference_feeds + self.spot_feeds:
            f.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _health_loop(self) -> None:
        while True:
            self.tick_health()
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ health
    def tick_health(self, now: float | None = None) -> QualityState:
        now = now or time.time()
        for f in self.reference_feeds + self.spot_feeds:
            f.update_staleness(now)
        ref = next((f for f in self.reference_feeds if not f.health.stale), None)
        spot = next((f for f in self.spot_feeds if not f.health.stale), None)
        new_ref = ref.name if ref else None
        new_spot = spot.name if spot else None
        if new_ref != self._active_ref:
            log.warning("reference feed switched: %s -> %s", self._active_ref,
                        new_ref or "SYNTHETIC (Coinbase-based estimate)")
            self._active_ref = new_ref
        if new_spot != self._active_spot:
            log.warning("primary spot feed switched: %s -> %s", self._active_spot, new_spot)
            self._active_spot = new_spot

        spot_prices = self.spot_prices()
        ref_px = self.reference(now)
        cl_spot = ref_px.spot if not ref_px.synthetic else None
        cl_twap = ref_px.twap if not ref_px.synthetic else None
        sec = int(now) - 1
        spot_twap = self.spot_twap(sec)
        if cl_spot and spot_prices:
            from statistics import median
            gap = (cl_spot - median(spot_prices.values())) / median(spot_prices.values()) * 1e4
            self.basis_bps += 0.02 * (gap - self.basis_bps)
        return self.quality.update(now, chainlink_spot=cl_spot, chainlink_twap=cl_twap,
                                   spot_prices=spot_prices, spot_twap=spot_twap,
                                   synthetic_reference=ref_px.synthetic)

    # ------------------------------------------------------------------ reads
    @property
    def active_spot(self) -> str | None:
        return self._active_spot

    @property
    def active_reference(self) -> str | None:
        return self._active_ref

    def spot_prices(self) -> dict[str, float]:
        return {f.name: f.health.last_price for f in self.spot_feeds if not f.health.stale}

    def primary_bars(self) -> SecondBars | None:
        return self.bars.get(self._active_spot) if self._active_spot else None

    def spot_twap(self, end_sec: int, window: int = 60) -> float | None:
        b = self.primary_bars()
        if b is None:
            return None
        w = b.window(end_sec, window)
        return float(w.close.mean()) if w is not None else None

    def reference(self, now: float | None = None) -> ReferencePrice:
        now = now or time.time()
        name = self._active_ref
        if name:
            s = self.ref_spot[name].latest()
            tw = self.ref_twap[name].latest()
            spot = s[1] if s else None
            twap = tw[1] if tw else None
            if twap is None and spot is not None:   # source without a TWAP channel
                twap = self.ref_spot[name].mean_between(int(now) - 60, int(now))
            if spot is not None or twap is not None:
                return ReferencePrice(spot, twap, name, False, now)
        # Fallback: synthetic Chainlink estimate from spot + last known basis.
        sp = self.spot_prices()
        if not sp or not self._active_spot:
            return ReferencePrice(None, None, "none", True, now)
        adj = 1 + self.basis_bps / 1e4
        spot = sp.get(self._active_spot, next(iter(sp.values()))) * adj
        twap = self.spot_twap(int(now) - 1)
        return ReferencePrice(spot, twap * adj if twap else None,
                              f"synthetic:{self._active_spot}", True, now)

    def reference_twap_at(self, sec: int) -> tuple[float, bool] | None:
        """Chainlink 60s TWAP value at a boundary second (for price-to-beat capture)."""
        for f in self.reference_feeds:
            v = self.ref_twap[f.name].get(sec)
            if v is not None:
                return v, False
        for f in self.reference_feeds:
            v = self.ref_spot[f.name].mean_between(sec - 59, sec)
            if v is not None and self.ref_spot[f.name].get(sec) is not None:
                return v, False
        b = self.primary_bars()
        if b is not None and b.last_sec is not None and b.last_sec >= sec:
            w = b.window(sec, 60)
            if w is not None:
                return float(w.close.mean()) * (1 + self.basis_bps / 1e4), True
        return None

    def reference_locked_mean(self, start_sec: int, end_sec: int) -> float | None:
        """Mean Chainlink spot over [start, end] (the part of the final TWAP already fixed)."""
        if self._active_ref:
            v = self.ref_spot[self._active_ref].mean_between(start_sec, end_sec)
            if v is not None:
                return v
        b = self.primary_bars()
        if b is None or end_sec < start_sec:
            return None
        w = b.window(end_sec, end_sec - start_sec + 1)
        return float(w.close.mean()) * (1 + self.basis_bps / 1e4) if w is not None else None

    def health(self) -> list[dict]:
        return [f.health.as_dict() for f in self.reference_feeds + self.spot_feeds]
