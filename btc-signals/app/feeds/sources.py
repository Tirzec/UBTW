"""Concrete price feeds. Each one only knows its vendor's wire format.

Add a new source by subclassing PriceFeed, implementing `stream`, and
registering it in FEED_TYPES; then reference its `type` in config.yaml.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Callable

import websockets

from .base import PriceFeed, PriceTick


def parse_iso(ts: str) -> float:
    """ISO-8601 -> unix seconds. Handles 'Z', nanosecond fractions and short fractions,
    and works on Python 3.9 (whose fromisoformat needs exactly 6 fraction digits)."""
    ts = ts.strip().replace("Z", "+00:00")
    if "." in ts:
        head, rest = ts.split(".", 1)
        n = 0
        while n < len(rest) and rest[n].isdigit():
            n += 1
        frac, tz = rest[:n], rest[n:]
        ts = f"{head}.{(frac + '000000')[:6]}{tz}"
    return datetime.fromisoformat(ts).timestamp()


async def _heartbeat(ws, text: str, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        await ws.send(text)


class _WSFeed(PriceFeed):
    ping_text: str | None = None
    ping_every: float = 10.0

    def subscribe_messages(self) -> list[dict]:
        return []

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        raise NotImplementedError

    async def on_open(self, ws) -> None:
        for m in self.subscribe_messages():
            await ws.send(json.dumps(m))

    async def stream(self, emit: Callable[[PriceTick], None]) -> None:
        async with websockets.connect(self.conf["url"], open_timeout=10, ping_interval=20,
                                      max_size=2**23, max_queue=4096) as ws:
            await self.on_open(ws)
            hb = asyncio.create_task(_heartbeat(ws, self.ping_text, self.ping_every)) \
                if self.ping_text else None
            try:
                async for raw in ws:
                    now = time.time()
                    self.mark_message()
                    if not raw or raw in ("PONG", "pong"):
                        continue
                    for t in self.parse(raw, now):
                        emit(t)
            finally:
                if hb:
                    hb.cancel()


# --------------------------------------------------------------------------
# Reference (Chainlink) feeds
# --------------------------------------------------------------------------
class PolymarketRTDSFeed(_WSFeed):
    """Legacy Polymarket RTDS: Chainlink BTC/USD spot + 60s TWAP. No auth. Deprecated.

    RTDS stops sending live updates when one connection carries two
    subscriptions (verified 2026-11), so each topic gets its own socket.
    The Chainlink snapshot frame arrives under topic "crypto_prices".
    """

    type_name = "polymarket_rtds"
    produces = ("spot", "twap")
    ping_text = "PING"
    ping_every = 5.0
    TOPICS = {"crypto_prices_chainlink": "spot", "crypto_prices_twap_sixty": "twap"}
    TOPIC_ALIASES = {"crypto_prices": "spot"}   # snapshot frame of the chainlink topic

    def subscribe_messages(self, topic: str | None = None) -> list[dict]:
        # RTDS matches the filter string literally: no spaces, or live updates never arrive.
        f = json.dumps({"symbol": "btc/usd"}, separators=(",", ":"))
        topics = [topic] if topic else list(self.TOPICS)
        return [{"action": "subscribe", "subscriptions": [{"topic": t, "type": "*", "filters": f}]}
                for t in topics]

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        try:
            d = json.loads(msg)
        except ValueError:
            return []
        topic = d.get("topic", "")
        kind = self.TOPICS.get(topic) or self.TOPIC_ALIASES.get(topic)
        p = d.get("payload") or {}
        if not kind or p.get("symbol", "btc/usd") != "btc/usd":
            return []
        win = 60 if kind == "twap" else 0
        rows = p["data"] if isinstance(p.get("data"), list) else [p]
        out = []
        for r in rows:
            if "value" not in r or "timestamp" not in r:
                continue
            out.append(PriceTick(self.name, kind, float(r["value"]), r["timestamp"] / 1000.0,
                                 now, window_s=win))
        return out

    async def _one(self, topic: str, emit: Callable[[PriceTick], None]) -> None:
        async with websockets.connect(self.conf["url"], open_timeout=10, ping_interval=20,
                                      max_queue=4096) as ws:
            for m in self.subscribe_messages(topic):
                await ws.send(json.dumps(m))
            hb = asyncio.create_task(_heartbeat(ws, self.ping_text, self.ping_every))
            try:
                async for raw in ws:
                    now = time.time()
                    self.mark_message()
                    if not raw or raw in ("PONG", "pong"):
                        continue
                    for t in self.parse(raw, now):
                        emit(t)
            finally:
                hb.cancel()

    async def stream(self, emit: Callable[[PriceTick], None]) -> None:
        tasks = [asyncio.create_task(self._one(t, emit)) for t in self.TOPICS]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()  # re-raise so the base class reconnects
        finally:
            for t in tasks:
                t.cancel()


class PolyBoltFeed(_WSFeed):
    """Polymarket PolyBolt live data (RTDS replacement). Requires CLOB API credentials."""

    type_name = "polybolt"
    produces = ("spot", "twap")
    CHANNELS = {"price.crypto": "spot", "price.crypto.twap": "twap"}

    def check_ready(self) -> str:
        s = self.secrets
        if not (s.get("poly_api_key") and s.get("poly_api_secret") and s.get("poly_api_passphrase")):
            return "POLY_API_KEY/SECRET/PASSPHRASE not set in .env"
        return ""

    async def on_open(self, ws) -> None:
        s = self.secrets
        await ws.send(json.dumps({"op": "auth", "auth": {
            "apiKey": s["poly_api_key"], "secret": s["poly_api_secret"],
            "passphrase": s["poly_api_passphrase"]}}))
        await ws.send(json.dumps({"op": "subscribe", "subscriptions": [
            {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
            {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}},
        ]}))

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        try:
            d = json.loads(msg)
        except ValueError:
            return []
        kind = self.CHANNELS.get(d.get("channel", ""))
        p = d.get("payload")
        if not kind or not p:
            return []
        rows = p.get("data") if isinstance(p, dict) and isinstance(p.get("data"), list) else (
            p if isinstance(p, list) else [p])
        out = []
        for r in rows:
            v = r.get("full_accuracy_value", r.get("value"))
            ts = r.get("timestamp", d.get("ts"))
            if v is None or ts is None:
                continue
            out.append(PriceTick(self.name, kind, float(v), float(ts) / 1000.0, now,
                                 window_s=60 if kind == "twap" else 0))
        return out


# --------------------------------------------------------------------------
# Spot exchange feeds (trades)
# --------------------------------------------------------------------------
class CoinbaseFeed(_WSFeed):
    type_name = "coinbase"
    produces = ("trade",)

    def subscribe_messages(self) -> list[dict]:
        sym = self.conf.get("symbol", "BTC-USD")
        return [{"type": "subscribe", "product_ids": [sym], "channels": ["matches", "heartbeat"]}]

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        d = json.loads(msg)
        if d.get("type") not in ("match", "last_match"):
            return []
        # Coinbase "side" is the MAKER side; a maker sell means a taker buy.
        side = 1 if d.get("side") == "sell" else -1
        return [PriceTick(self.name, "trade", float(d["price"]), parse_iso(d["time"]), now,
                          size=float(d["size"]), side=side)]


class KrakenFeed(_WSFeed):
    type_name = "kraken"
    produces = ("trade",)

    def subscribe_messages(self) -> list[dict]:
        sym = self.conf.get("symbol", "BTC/USD")
        return [{"method": "subscribe", "params": {"channel": "trade", "symbol": [sym]}}]

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        d = json.loads(msg)
        if d.get("channel") != "trade":
            return []
        out = []
        for r in d.get("data", []):
            out.append(PriceTick(self.name, "trade", float(r["price"]), parse_iso(r["timestamp"]),
                                 now, size=float(r["qty"]), side=1 if r["side"] == "buy" else -1))
        return out


class BinanceFeed(_WSFeed):
    type_name = "binance"
    produces = ("trade",)

    def subscribe_messages(self) -> list[dict]:
        sym = self.conf.get("symbol", "btcusdt").lower()
        return [{"method": "SUBSCRIBE", "params": [f"{sym}@trade"], "id": 1}]

    def parse(self, msg: str, now: float) -> list[PriceTick]:
        d = json.loads(msg)
        if d.get("e") != "trade":
            return []
        # m = buyer is maker -> taker sold
        return [PriceTick(self.name, "trade", float(d["p"]), d["T"] / 1000.0, now,
                          size=float(d["q"]), side=-1 if d.get("m") else 1)]


FEED_TYPES: dict[str, type[PriceFeed]] = {
    c.type_name: c for c in (PolymarketRTDSFeed, PolyBoltFeed, CoinbaseFeed, KrakenFeed, BinanceFeed)
}


def build_feed(conf: dict, role: str, secrets: dict, health_conf: dict) -> PriceFeed:
    t = conf.get("type")
    if t not in FEED_TYPES:
        raise ValueError(f"unknown feed type {t!r}; known: {sorted(FEED_TYPES)}")
    return FEED_TYPES[t](conf, role, secrets, health_conf)
