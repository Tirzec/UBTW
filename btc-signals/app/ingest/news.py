"""Crypto news via RSS, classified bullish / bearish / neutral with a confidence.

The classifier is a transparent weighted lexicon (no black box): every match
is listed in `hits` so you can see why a headline got its label.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("news")

BULLISH = {
    "etf approval": 2.0, "approves": 1.0, "approved": 1.0, "inflow": 1.2, "inflows": 1.2,
    "all-time high": 1.5, "record high": 1.5, "rate cut": 1.5, "cuts rates": 1.5,
    "adopts": 1.0, "adoption": 0.8, "buys bitcoin": 1.5, "accumulat": 1.0, "surge": 1.2,
    "surges": 1.2, "rally": 1.2, "rallies": 1.2, "soar": 1.2, "soars": 1.2, "jumps": 1.0,
    "bullish": 1.0, "strategic reserve": 1.5, "breakout": 0.8, "gains": 0.6, "rebound": 0.8,
    "short squeeze": 1.2, "dovish": 1.0, "easing": 0.6, "cooler-than-expected": 1.2,
}
BEARISH = {
    "hack": 2.0, "hacked": 2.0, "exploit": 1.8, "ban": 1.5, "bans": 1.5, "lawsuit": 1.0,
    "sues": 1.0, "charges": 0.8, "outflow": 1.2, "outflows": 1.2, "plunge": 1.5,
    "plunges": 1.5, "crash": 1.8, "crashes": 1.8, "liquidat": 1.0, "selloff": 1.2,
    "sell-off": 1.2, "bearish": 1.0, "rate hike": 1.5, "hikes rates": 1.5, "bankrupt": 2.0,
    "insolven": 2.0, "fraud": 1.5, "seize": 1.0, "seized": 1.0, "tumble": 1.2, "tumbles": 1.2,
    "slump": 1.2, "slumps": 1.2, "drops": 0.8, "falls": 0.8, "dumps": 1.2, "hawkish": 1.0,
    "hotter-than-expected": 1.2, "delist": 1.2, "halts withdrawals": 2.0, "depeg": 1.8,
}
RELEVANT = ("bitcoin", "btc", "crypto", "fed", "fomc", "powell", "sec", "etf", "cpi",
            "inflation", "jobs", "payrolls", "stablecoin", "tether", "binance", "coinbase")
NEGATIONS = ("not ", "no ", "denies", "rejects", "fails to", "won't", "despite")


@dataclass
class Headline:
    title: str
    link: str
    source: str
    published: float
    label: str = "neutral"          # bullish | bearish | neutral
    score: float = 0.0              # signed, [-1, 1]
    confidence: float = 0.0         # [0, 1]
    hits: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"title": self.title, "link": self.link, "source": self.source,
                "published": self.published, "label": self.label,
                "score": round(self.score, 3), "confidence": round(self.confidence, 3),
                "hits": self.hits}


def classify(title: str) -> tuple[str, float, float, list[str]]:
    """Return (label, signed score in [-1,1], confidence in [0,1], matched terms)."""
    t = " " + title.lower() + " "
    hits: list[str] = []
    raw = 0.0
    for terms, sign in ((BULLISH, 1.0), (BEARISH, -1.0)):
        for term, w in terms.items():
            if re.search(r"(?<![a-z])" + re.escape(term), t):
                hits.append(("+" if sign > 0 else "-") + term)
                raw += sign * w
    negated = any(n in t for n in NEGATIONS)
    if negated:
        raw *= 0.3
    relevant = any(re.search(r"(?<![a-z])" + k + r"(?![a-z])", t) for k in RELEVANT)
    score = math.tanh(raw / 2.0)
    conf = abs(score) * (1.0 if relevant else 0.4) * (0.6 if negated else 1.0)
    if len(hits) >= 2 and all(h[0] == hits[0][0] for h in hits):
        conf = min(1.0, conf * 1.15)
    label = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
    if label == "neutral":
        conf = min(conf, 0.3)
    return label, score, conf, hits


def parse_rss(xml_text: str, source: str) -> list[Headline]:
    out: list[Headline] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    items = root.iter("item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            ts = parsedate_to_datetime(pub).timestamp() if pub else time.time()
        except (TypeError, ValueError):
            ts = time.time()
        if title:
            h = Headline(title, link or title, source, ts)
            h.label, h.score, h.confidence, h.hits = classify(title)
            out.append(h)
    return out


def sentiment_score(heads: list[Headline], now: float, halflife_min: float) -> float:
    """Exponentially decayed, confidence-weighted sentiment in [-1, 1]."""
    tot = 0.0
    for h in heads:
        age_min = max(0.0, (now - h.published) / 60.0)
        tot += h.score * h.confidence * 0.5 ** (age_min / halflife_min)
    return max(-1.0, min(1.0, tot))


class NewsFeed:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None, on_new=None):
        nc = cfg.get("news", {})
        self.enabled = nc.get("enabled", True)
        self.feeds = nc.get("feeds", [])
        self.interval = float(nc.get("poll_interval_s", 60))
        self.halflife = float(nc.get("sentiment_halflife_min", 30))
        self.shock_conf = float(nc.get("shock_confidence", 0.8))
        self.shock_min = float(nc.get("shock_block_min", 5))
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=True,
                                                  headers={"User-Agent": "btc-signals/1.0"})
        self.items: dict[str, Headline] = {}
        self.on_new = on_new
        self.errors: dict[str, str] = {}

    def recent(self, now: float | None = None, hours: float = 6) -> list[Headline]:
        now = now or time.time()
        return sorted((h for h in self.items.values() if now - h.published < hours * 3600),
                      key=lambda h: -h.published)

    def score(self, now: float | None = None) -> float:
        now = now or time.time()
        return sentiment_score(self.recent(now), now, self.halflife)

    def shock(self, now: float | None = None) -> Headline | None:
        now = now or time.time()
        for h in self.recent(now, hours=1):
            if h.label != "neutral" and h.confidence >= self.shock_conf \
                    and 0 <= now - h.published < self.shock_min * 60:
                return h
        return None

    async def poll_once(self) -> None:
        for url in self.feeds:
            try:
                r = await self.client.get(url)
                r.raise_for_status()
                src = httpx.URL(url).host
                for h in parse_rss(r.text, src):
                    if h.link not in self.items:
                        self.items[h.link] = h
                        if self.on_new and time.time() - h.published < 3600:
                            self.on_new(h)
                self.errors.pop(url, None)
            except Exception as e:  # noqa: BLE001
                self.errors[url] = f"{type(e).__name__}: {e}"[:150]
        cutoff = time.time() - 24 * 3600
        for k in [k for k, h in self.items.items() if h.published < cutoff]:
            del self.items[k]

    async def run(self) -> None:
        if not self.enabled:
            return
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)
