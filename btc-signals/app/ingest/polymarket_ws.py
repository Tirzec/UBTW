"""Live Polymarket order books from the CLOB market websocket (REST fallback)."""
from __future__ import annotations

import asyncio
import json
from collections import deque
import logging
import time

import httpx
import websockets

log = logging.getLogger("clob")


class OrderBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated = 0.0          # when we received this book (freshness)
        self.exch_ts = 0.0          # exchange transactTime of the snapshot (ordering), if known
        self.last_trade: float | None = None
        self.trades: deque[tuple[float, float, float]] = deque(maxlen=500)  # (ts, price, size)

    def snapshot(self, bids: list, asks: list, ts: float) -> None:
        self.bids = {float(x["price"]): float(x["size"]) for x in bids if float(x["size"]) > 0}
        self.asks = {float(x["price"]): float(x["size"]) for x in asks if float(x["size"]) > 0}
        self.updated = ts

    def level(self, side: str, price: float, size: float, ts: float) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.updated = ts

    def sorted(self) -> dict:
        return {"bids": sorted(self.bids.items(), key=lambda kv: -kv[0]),
                "asks": sorted(self.asks.items(), key=lambda kv: kv[0]),
                "updated": self.updated, "exch_ts": self.exch_ts, "last_trade": self.last_trade}


class BookStore:
    """Applies CLOB websocket messages. `apply` is pure w.r.t. the network (unit-tested)."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self.listeners: list = []          # called (no args) after every book update: event-driven entries

    def notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener must never break the feed
                pass

    def book(self, asset_id: str) -> OrderBook:
        b = self.books.get(asset_id)
        if b is None:
            b = self.books[asset_id] = OrderBook()
        return b

    def apply(self, msg: dict | list, now: float | None = None) -> None:
        now = now or time.time()
        if isinstance(msg, list):
            for m in msg:
                self.apply(m, now)
            return
        et = msg.get("event_type")
        if "bids" in msg and "asks" in msg and msg.get("asset_id"):
            self.book(msg["asset_id"]).snapshot(msg["bids"], msg["asks"], now)
        elif et == "price_change" or "price_changes" in msg:
            for ch in msg.get("price_changes", []):
                self.book(ch["asset_id"]).level(ch["side"], float(ch["price"]),
                                                float(ch["size"]), now)
        elif et == "last_trade_price" and msg.get("asset_id"):
            b = self.book(msg["asset_id"])
            b.last_trade = float(msg["price"])
            b.trades.append((now, float(msg["price"]), float(msg.get("size") or 0)))
            b.updated = now
        self.notify()

    def trades_since(self, asset_id: str, since: float) -> list[tuple[float, float, float]]:
        b = self.books.get(asset_id)
        return [t for t in b.trades if t[0] >= since] if b else []

    def get(self, asset_id: str) -> dict | None:
        b = self.books.get(asset_id)
        return b.sorted() if b and b.updated else None


class ClobMarketStream:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        pc = cfg.get("polymarket", {})
        self.url = pc.get("clob_ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.rest = pc.get("clob_url", "https://clob.polymarket.com")
        self.store = BookStore()
        self.client = client or httpx.AsyncClient(timeout=10)
        self._wanted: set[str] = set()
        self._changed = asyncio.Event()
        self.connected = False
        self.reconnects = 0
        self.last_msg = 0.0
        self.last_error = ""

    def set_assets(self, assets: set[str]) -> None:
        assets = {a for a in assets if a}
        if assets != self._wanted:
            self._wanted = assets
            self._changed.set()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            if not self._wanted:
                await asyncio.sleep(1)
                continue
            try:
                await self._session(sorted(self._wanted))
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                self.reconnects += 1
                log.warning("CLOB websocket error: %s", self.last_error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            self.connected = False

    async def _session(self, assets: list[str]) -> None:
        self._changed.clear()
        # max_queue: the default (16 frames) stops reading during bursts of book updates,
        # and the server then disconnects us with 1013 "slow consumer".
        async with websockets.connect(self.url, open_timeout=10, ping_interval=None,
                                      max_size=2**23, max_queue=8192) as ws:
            await ws.send(json.dumps({"assets_ids": assets, "type": "market",
                                      "custom_feature_enabled": True}))
            self.connected = True

            async def heartbeat():
                while True:
                    await asyncio.sleep(10)
                    await ws.send("PING")

            hb = asyncio.create_task(heartbeat())
            try:
                while not self._changed.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    self.last_msg = time.time()
                    if raw == "PONG" or not raw:
                        continue
                    try:
                        self.store.apply(json.loads(raw))
                    except (ValueError, KeyError) as e:
                        log.debug("bad clob msg %s: %s", e, raw[:200])
            finally:
                hb.cancel()

    async def refresh_rest(self, asset_id: str) -> None:
        """Fallback when a book has gone quiet."""
        r = await self.client.get(f"{self.rest}/book", params={"token_id": asset_id})
        r.raise_for_status()
        d = r.json()
        self.store.book(asset_id).snapshot(d.get("bids", []), d.get("asks", []), time.time())
