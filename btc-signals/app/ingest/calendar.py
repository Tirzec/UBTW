"""Economic calendar (CPI, FOMC, NFP, ...) -> NO TRADE blackout windows."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("calendar")


@dataclass
class EconEvent:
    title: str
    ts: float
    country: str = "USD"
    impact: str = "High"
    source: str = "feed"

    def as_dict(self) -> dict:
        return {"title": self.title, "ts": self.ts, "country": self.country,
                "impact": self.impact, "source": self.source,
                "time": datetime.fromtimestamp(self.ts).astimezone().isoformat(timespec="minutes")}


def parse_ff(items: list[dict], countries: list[str], impacts: list[str]) -> list[EconEvent]:
    out = []
    for e in items:
        if e.get("country") not in countries or e.get("impact") not in impacts:
            continue
        try:
            ts = datetime.fromisoformat(e["date"]).timestamp()
        except (KeyError, ValueError):
            continue
        out.append(EconEvent(e.get("title", "?"), ts, e["country"], e["impact"], "feed"))
    return out


def load_local(path: Path) -> list[EconEvent]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    out = []
    for e in data.get("events", []) or []:
        try:
            ts = datetime.fromisoformat(str(e["time"])).timestamp()
        except (KeyError, ValueError):
            log.warning("bad event in %s: %r", path, e)
            continue
        out.append(EconEvent(e.get("title", "event"), ts, e.get("country", "USD"),
                             e.get("impact", "High"), "local"))
    return out


def active_blackouts(events: list[EconEvent], now: float, before_min: float,
                     after_min: float) -> list[EconEvent]:
    return [e for e in events if e.ts - before_min * 60 <= now <= e.ts + after_min * 60]


def blackout_overlaps(events: list[EconEvent], start: float, end: float, before_min: float,
                      after_min: float) -> list[EconEvent]:
    """Events whose blackout overlaps [start, end] (e.g. the remainder of a window)."""
    return [e for e in events if e.ts - before_min * 60 <= end and e.ts + after_min * 60 >= start]


class EconCalendar:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        cc = cfg.get("calendar", {})
        self.enabled = cc.get("enabled", True)
        self.url = cc.get("feed_url")
        self.refresh_s = float(cc.get("refresh_interval_s", 3600))
        root = Path(cfg.get("_root", "."))
        self.local_path = root / cc.get("local_file", "events.yaml")
        self.countries = cc.get("countries", ["USD"])
        self.impacts = cc.get("impacts", ["High"])
        self.before = float(cc.get("blackout_before_min", 5))
        self.after = float(cc.get("blackout_after_min", 10))
        self.client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "btc-signals/1.0"})
        self.feed_events: list[EconEvent] = []
        self.error = ""
        self.updated = 0.0

    @property
    def events(self) -> list[EconEvent]:
        evs = {(e.title, int(e.ts)): e for e in self.feed_events + load_local(self.local_path)}
        return sorted(evs.values(), key=lambda e: e.ts)

    def blackouts(self, now: float | None = None) -> list[EconEvent]:
        if not self.enabled:
            return []
        return active_blackouts(self.events, now or time.time(), self.before, self.after)

    def upcoming(self, now: float | None = None, n: int = 5) -> list[EconEvent]:
        now = now or time.time()
        return [e for e in self.events if e.ts + self.after * 60 >= now][:n]

    async def refresh(self) -> None:
        if not self.url:
            return
        r = await self.client.get(self.url)
        r.raise_for_status()
        self.feed_events = parse_ff(r.json(), self.countries, self.impacts)
        self.updated = time.time()

    async def run(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                await self.refresh()
                self.error = ""
                wait = self.refresh_s
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"[:200]
                log.warning("calendar refresh failed (keeping %d cached events): %s",
                            len(self.feed_events), self.error)
                wait = 300   # feed is rate limited; retry later
            await asyncio.sleep(wait)
