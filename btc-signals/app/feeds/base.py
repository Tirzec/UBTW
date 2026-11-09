"""The single price-feed interface every source implements.

A feed's only job is to turn a vendor stream into PriceTick objects and hand
them to `emit`. Reconnects, backoff and health/staleness tracking live here so
that every source behaves the same and can be swapped from config.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Literal

log = logging.getLogger("feeds")

TickKind = Literal["trade", "spot", "twap"]


@dataclass(slots=True)
class PriceTick:
    source: str            # feed name, e.g. "coinbase", "polymarket_rtds"
    kind: TickKind         # trade (exchange print), spot (oracle price), twap (oracle TWAP)
    price: float
    ts: float              # source timestamp, unix seconds
    received: float        # local receive time, unix seconds
    size: float = 0.0      # trades only
    side: int = 0          # +1 taker buy, -1 taker sell, 0 unknown
    window_s: int = 0      # TWAP window, twap ticks only


@dataclass
class FeedHealth:
    name: str
    role: str
    connected: bool = False
    last_msg: float = 0.0          # any message incl. heartbeats
    last_tick: float = 0.0         # last PriceTick received (local time)
    last_tick_ts: float = 0.0      # last PriceTick source timestamp
    last_price: float = 0.0
    last_change: float = 0.0       # local time the price last changed
    ticks: int = 0
    reconnects: int = 0
    last_error: str = ""
    disabled_reason: str = ""      # e.g. missing credentials
    stale: bool = True
    stale_reason: str = "no data yet"
    _last_warn: float = field(default=0.0, repr=False)

    def as_dict(self) -> dict:
        now = time.time()
        return {
            "name": self.name,
            "role": self.role,
            "connected": self.connected,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "age_s": round(now - self.last_tick, 1) if self.last_tick else None,
            "lag_s": round(self.last_tick - self.last_tick_ts, 2) if self.last_tick else None,
            "last_price": self.last_price,
            "ticks": self.ticks,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "disabled_reason": self.disabled_reason,
        }


def evaluate_staleness(h: FeedHealth, now: float, stale_after_s: float, max_lag_s: float,
                       frozen_after_s: float, no_price_after_s: float | None = None
                       ) -> tuple[bool, str]:
    """Pure staleness rule, shared by every feed (unit-tested).

    stale_after_s    - no message at all (incl. heartbeats): feed not responding
    no_price_after_s - no price tick (trade feeds can be quiet; heartbeats keep them alive)
    max_lag_s        - source timestamp too far behind local receive time
    frozen_after_s   - price has not changed (stuck oracle)
    """
    no_price_after_s = stale_after_s if no_price_after_s is None else no_price_after_s
    if h.disabled_reason:
        return True, h.disabled_reason
    if not h.last_tick:
        return True, "no data yet"
    last_any = max(h.last_msg, h.last_tick)
    if now - last_any > stale_after_s:
        return True, f"not responding for {now - last_any:.0f}s"
    if now - h.last_tick > no_price_after_s:
        return True, f"no price for {now - h.last_tick:.0f}s"
    if h.last_tick - h.last_tick_ts > max_lag_s:
        return True, f"data lagging {h.last_tick - h.last_tick_ts:.1f}s behind source"
    if frozen_after_s and h.last_change and now - h.last_change > frozen_after_s:
        return True, f"price frozen for {now - h.last_change:.0f}s"
    return False, ""


class PriceFeed(ABC):
    """Base class. Subclasses implement `stream(emit)` for one connection."""

    type_name: str = "base"
    produces: tuple[TickKind, ...] = ()

    def __init__(self, conf: dict, role: str, secrets: dict | None = None,
                 health_conf: dict | None = None):
        self.conf = conf
        self.role = role
        self.secrets = secrets or {}
        self.name = conf.get("name", self.type_name)
        self.health = FeedHealth(name=self.name, role=role)
        hc = {**(health_conf or {}), **{k: v for k, v in conf.items() if k in (
            "stale_after_s", "no_price_after_s", "max_lag_s", "frozen_after_s")}}
        self.stale_after_s = float(hc.get("stale_after_s", 5))
        self.no_price_after_s = float(hc.get("no_price_after_s", self.stale_after_s))
        self.max_lag_s = float(hc.get("max_lag_s", 5))
        self.frozen_after_s = float(hc.get("frozen_after_s", 30))
        self.warn_repeat_s = float(hc.get("warn_repeat_s", 60))
        self.max_backoff = float(hc.get("reconnect_max_backoff_s", 30))
        self._stopping = False

    # -- to implement -----------------------------------------------------
    def check_ready(self) -> str:
        """Return a reason string if this feed cannot run (e.g. no credentials)."""
        return ""

    @abstractmethod
    async def stream(self, emit: Callable[[PriceTick], None]) -> None:
        """Connect once and emit ticks until the connection ends or errors."""

    # -- shared machinery -------------------------------------------------
    def mark_message(self) -> None:
        self.health.last_msg = time.time()

    def _wrap_emit(self, sink: Callable[[PriceTick], None]) -> Callable[[PriceTick], None]:
        def emit(t: PriceTick) -> None:
            h = self.health
            h.last_msg = h.last_tick = t.received
            h.last_tick_ts = t.ts
            if t.price != h.last_price or not h.last_change:
                h.last_change = t.received
            h.last_price = t.price
            h.ticks += 1
            sink(t)
        return emit

    async def run(self, sink: Callable[[PriceTick], None]) -> None:
        reason = self.check_ready()
        if reason:
            self.health.disabled_reason = reason
            log.warning("feed %s (%s) disabled: %s", self.name, self.role, reason)
            return
        emit = self._wrap_emit(sink)
        backoff = 1.0
        while not self._stopping:
            try:
                self.health.connected = True
                started = time.time()
                await self.stream(emit)
                self.health.last_error = "connection closed"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any vendor error -> reconnect
                self.health.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("feed %s error: %s", self.name, self.health.last_error)
                started = 0.0
            self.health.connected = False
            self.health.reconnects += 1
            if started and time.time() - started > 60:
                backoff = 1.0
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff = min(backoff * 2, self.max_backoff)

    def update_staleness(self, now: float | None = None) -> bool:
        """Recompute stale flag; log a clear warning on transitions (and periodically)."""
        now = now or time.time()
        h = self.health
        stale, why = evaluate_staleness(h, now, self.stale_after_s, self.max_lag_s,
                                        self.frozen_after_s, self.no_price_after_s)
        if stale and not h.disabled_reason:
            if not h.stale or now - h._last_warn > self.warn_repeat_s:
                log.warning("PRICE FEED STALE: %s (%s) - %s", self.name, self.role, why)
                h._last_warn = now
        elif not stale and h.stale and h.ticks:
            log.info("price feed recovered: %s (%s)", self.name, self.role)
        h.stale, h.stale_reason = stale, why
        return stale

    def stop(self) -> None:
        self._stopping = True
