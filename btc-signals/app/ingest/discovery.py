"""Find the live BTC 5m/15m markets via Gamma, parse their rules, track outcomes.

Slugs are deterministic: btc-updown-{5m|15m}-{window start unix}.
Observed live: priceToBeat(window N) == finalPrice(window N-1) (both are the
Chainlink 60s TWAP at the boundary), exposed in event.eventMetadata.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from ..rules import RuleSpec, parse_rules, validate_rules

log = logging.getLogger("discovery")

DURATION_SECONDS = {"5m": 300, "15m": 900}
SITE_VARIANT = {"5m": "fiveminute", "15m": "fifteen"}


def window_start(now: float, duration: str) -> int:
    s = DURATION_SECONDS[duration]
    return int(now // s * s)


def slug_for(duration: str, start: int) -> str:
    return f"btc-updown-{duration}-{start}"


@dataclass
class Window:
    duration: str
    start: int
    end: int
    slug: str
    title: str = ""
    condition_id: str = ""
    token_up: str = ""
    token_down: str = ""
    fee_schedule: dict = field(default_factory=dict)
    tick_size: float = 0.01
    min_size: float = 5
    size_step: float = 0.01
    accepting_orders: bool = False
    rules: RuleSpec | None = None
    found: bool = False
    # price to beat
    price_to_beat: float | None = None
    ptb_source: str = ""           # chainlink | gamma | prev_final | synthetic
    gamma_ptb: float | None = None
    ptb_check_bps: float | None = None
    # resolution
    final_price: float | None = None
    outcome: str | None = None     # "UP" | "DOWN"
    last_fetch: float = 0.0
    # Display-only: Polymarket's site price API returns the Chainlink *spot* at the
    # boundary. It is NOT the resolution price (60s TWAP); it disagreed with the actual
    # outcome in 15 of 138 windows checked on 2026-11-09.
    site_open: float | None = None
    url: str = ""
    venue: str = "polymarket_intl"

    @property
    def lookback_s(self) -> int:
        return (self.rules.twap_lookback_s if self.rules and self.rules.twap_lookback_s else 60)

    def seconds_left(self, now: float) -> float:
        return self.end - now

    def as_dict(self, now: float | None = None) -> dict:
        now = now or time.time()
        return {
            "duration": self.duration, "slug": self.slug, "title": self.title,
            "start": self.start, "end": self.end, "seconds_left": round(self.end - now, 1),
            "condition_id": self.condition_id, "found": self.found,
            "accepting_orders": self.accepting_orders,
            "price_to_beat": self.price_to_beat, "ptb_source": self.ptb_source,
            "gamma_ptb": self.gamma_ptb, "ptb_check_bps": self.ptb_check_bps,
            "final_price": self.final_price, "outcome": self.outcome,
            "site_open_spot": self.site_open,
            "rules_ok": self.rules.ok if self.rules else None,
            "rules_mismatches": self.rules.mismatches if self.rules else ["rules not loaded"],
            "twap_lookback_s": self.lookback_s, "fee_schedule": self.fee_schedule,
            "url": self.url or f"https://polymarket.com/event/{self.slug}", "venue": self.venue,
        }


def apply_event(w: Window, ev: dict, expected_rules: dict) -> Window:
    """Fill a Window from a Gamma event payload (pure, unit-tested)."""
    markets = ev.get("markets") or []
    if not markets:
        return w
    m = markets[0]
    w.found = True
    w.title = ev.get("title") or m.get("question", "")
    w.condition_id = m.get("conditionId", "")
    tokens = m.get("clobTokenIds")
    tokens = json.loads(tokens) if isinstance(tokens, str) else (tokens or [])
    outcomes = m.get("outcomes")
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
    if len(tokens) == 2 and len(outcomes) == 2:
        idx = {o.lower(): i for i, o in enumerate(outcomes)}
        w.token_up = tokens[idx.get("up", 0)]
        w.token_down = tokens[idx.get("down", 1)]
    w.fee_schedule = m.get("feeSchedule") or {}
    if not m.get("feesEnabled", True):
        w.fee_schedule = {"rate": 0.0, "exponent": 1}
    w.tick_size = float(m.get("orderPriceMinTickSize") or 0.01)
    w.min_size = float(m.get("orderMinSize") or 5)
    w.accepting_orders = bool(m.get("acceptingOrders")) and not m.get("closed")
    w.rules = validate_rules(parse_rules(m), expected_rules, w.duration)
    if len(outcomes) != 2 or {o.lower() for o in outcomes} != {"up", "down"}:
        w.rules.mismatches.append(f"unexpected outcomes {outcomes}")
    meta = ev.get("eventMetadata") or {}
    if meta.get("priceToBeat") is not None:
        w.gamma_ptb = float(meta["priceToBeat"])
    if meta.get("finalPrice") is not None:
        w.final_price = float(meta["finalPrice"])
    if m.get("closed"):
        prices = m.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
        if len(prices) == 2 and len(outcomes) == 2:
            win = [o for o, p in zip(outcomes, prices) if float(p) >= 0.99]
            if win:
                w.outcome = win[0].upper()
    if w.outcome is None and w.final_price is not None and w.gamma_ptb is not None \
            and m.get("closed"):
        w.outcome = "UP" if w.final_price >= w.gamma_ptb else "DOWN"
    return w


class Discovery:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        pc = cfg.get("polymarket", {})
        self.gamma = pc.get("gamma_url", "https://gamma-api.polymarket.com")
        self.durations: list[str] = pc.get("durations", ["5m", "15m"])
        self.interval = float(pc.get("discovery_interval_s", 15))
        self.expected = pc.get("expected_rules", {})
        self.site_url = pc.get("site_price_url", "https://polymarket.com/api/crypto/crypto-price")
        self.client = client or httpx.AsyncClient(timeout=10)
        self.windows: dict[str, Window] = {}
        self.on_settled = None        # callback(Window)
        self.last_error = ""

    async def fetch_event(self, slug: str) -> dict | None:
        r = await self.client.get(f"{self.gamma}/events/slug/{slug}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get(self, duration: str, start: int) -> Window:
        slug = slug_for(duration, start)
        w = self.windows.get(slug)
        if w is None:
            w = Window(duration, start, start + DURATION_SECONDS[duration], slug)
            self.windows[slug] = w
        return w

    def current(self, duration: str, now: float | None = None) -> Window:
        return self.get(duration, window_start(now or time.time(), duration))

    def previous(self, duration: str, now: float | None = None) -> Window:
        return self.get(duration, window_start(now or time.time(), duration)
                        - DURATION_SECONDS[duration])

    def next(self, duration: str, now: float | None = None) -> Window:
        return self.get(duration, window_start(now or time.time(), duration)
                        + DURATION_SECONDS[duration])

    async def refresh(self, w: Window) -> None:
        ev = await self.fetch_event(w.slug)
        w.last_fetch = time.time()
        if ev:
            apply_event(w, ev, self.expected)
        if self.site_url and w.site_open is None and w.start <= time.time() < w.end:
            await self.fetch_site_open(w)

    async def fetch_site_open(self, w: Window) -> None:
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        try:
            r = await self.client.get(self.site_url, params={
                "symbol": "BTC", "eventStartTime": iso(w.start), "endDate": iso(w.end),
                "variant": SITE_VARIANT[w.duration]})
            if r.status_code == 200 and r.json().get("openPrice") is not None:
                w.site_open = float(r.json()["openPrice"])
        except (httpx.HTTPError, ValueError):
            pass

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("discovery error: %s", self.last_error)
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        now = time.time()
        jobs = []
        for d in self.durations:
            cur, nxt, prev = self.current(d, now), self.next(d, now), self.previous(d, now)
            for w in (cur, nxt):
                # refresh live windows often (acceptingOrders, gamma priceToBeat)
                if not w.found or now - w.last_fetch > 60 or (w.gamma_ptb is None and w is cur):
                    jobs.append(self.refresh(w))
            if prev.gamma_ptb is None or prev.final_price is None:
                jobs.append(self.refresh(prev))
        # settle anything ended and unresolved (up to 2h back)
        for w in list(self.windows.values()):
            if w.end < now - 5 and w.outcome is None and w.end > now - 7200 \
                    and now - w.last_fetch > 20:
                jobs.append(self.refresh(w))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        for w in list(self.windows.values()):
            if w.outcome and self.on_settled and not getattr(w, "_settled_cb", False):
                w._settled_cb = True  # type: ignore[attr-defined]
                self.on_settled(w)
            if w.end < now - 3 * 3600:
                del self.windows[w.slug]
