"""Markets WebSocket client against a local server: signed handshake, subscribe, books into the store."""
import asyncio
import base64
import json

import websockets

from app.ingest.polymarket_ws import BookStore
from app.ingest.us_markets import UsBookStream, us_book_to_store

SECRET = base64.b64encode(bytes(range(64))).decode()


def test_stream_subscribes_and_updates_books():
    got = {"headers": None, "subs": []}

    async def handler(ws, path=None):
        req = getattr(ws, "request", None)
        hdrs = req.headers if req is not None else ws.request_headers
        got["headers"] = {k: hdrs.get(k) for k in ("X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature")}
        sub = json.loads(await ws.recv())
        got["subs"].append(sub)
        await ws.send(json.dumps({"heartbeat": {}}))
        await ws.send(json.dumps({"requestId": sub["subscribe"]["requestId"], "marketData": {
            "marketSlug": "m1", "bids": [{"px": {"value": "0.55"}, "qty": "10"}],
            "offers": [{"px": {"value": "0.57"}, "qty": "4"}], "transactTime": "2026-11-10T00:00:00.5Z"}}))
        await asyncio.sleep(0.3)

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = list(srv.sockets)[0].getsockname()[1]
            store = BookStore()
            s = UsBookStream(store, {}, "kid", SECRET, url=f"ws://127.0.0.1:{port}")
            s.set_slugs({"m1"})
            task = asyncio.ensure_future(s.run())
            for _ in range(50):
                await asyncio.sleep(0.05)
                if s.messages:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return store, s

    store, s = asyncio.run(run())
    assert got["headers"]["X-PM-Access-Key"] == "kid" and got["headers"]["X-PM-Signature"]
    sub = got["subs"][0]["subscribe"]
    assert sub["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA" and sub["marketSlugs"] == ["m1"]
    assert s.messages == 1
    up, down = store.get("m1:long"), store.get("m1:short")
    assert up["bids"][0] == (0.55, 10.0) and up["asks"][0] == (0.57, 4.0)
    assert down["bids"][0] == (0.43, 4.0)                      # Down bid = 1 - Up ask


def test_newest_book_wins():
    store = BookStore()
    new = {"marketData": {"bids": [{"px": {"value": "0.60"}, "qty": "1"}], "offers": [{"px": {"value": "0.61"}, "qty": "1"}],
                          "transactTime": "2026-11-10T00:00:05Z"}}
    old = {"marketData": {"bids": [{"px": {"value": "0.40"}, "qty": "1"}], "offers": [{"px": {"value": "0.41"}, "qty": "1"}],
                          "transactTime": "2026-11-10T00:00:01Z"}}
    us_book_to_store(store, "m", new, 2e9, {})
    us_book_to_store(store, "m", old, 2e9, {})                  # a slower poll arriving late is ignored
    assert store.get("m:long")["bids"][0][0] == 0.60


def test_book_freshness_is_receive_time_and_order_is_exchange_time():
    import calendar
    exch = calendar.timegm((2026, 11, 10, 0, 0, 0))
    store = BookStore()
    snap = lambda px, tt: {"marketData": {"bids": [{"px": {"value": px}, "qty": "1"}],          # noqa: E731
                                          "offers": [{"px": {"value": "0.99"}, "qty": "1"}], "transactTime": tt}}
    us_book_to_store(store, "m", snap("0.60", "2026-11-10T00:00:00Z"), exch + 1600, {})   # quiet book, polled now
    b = store.get("m:long")
    assert b["updated"] == exch + 1600 and b["exch_ts"] == exch     # fresh (received now), changed 1600 s ago
    us_book_to_store(store, "m", snap("0.40", "2026-11-09T23:59:00Z"), exch + 1601, {})   # older snapshot, late
    assert store.get("m:long")["bids"][0][0] == 0.60                 # ignored: newest exchange time wins


def test_bad_websocket_frame_does_not_drop_the_connection():
    async def handler(ws, path=None):
        await ws.recv()
        await ws.send("not json at all")
        await ws.send(json.dumps({"marketData": "x"}))                    # AttributeError shape
        await ws.send(json.dumps({"marketData": {"marketSlug": "m1", "bids": ["0.5"], "offers": []}}))
        await ws.send(json.dumps({"marketData": {"marketSlug": "m1", "bids": [{"px": {"value": "0.5"}, "qty": "1"}],
                                                 "offers": [{"px": {"value": "0.6"}, "qty": "1"}],
                                                 "transactTime": "2026-11-10T00:00:00Z"}}))
        await asyncio.sleep(0.3)

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = list(srv.sockets)[0].getsockname()[1]
            s = UsBookStream(BookStore(), {}, "kid", SECRET, url=f"ws://127.0.0.1:{port}")
            s.set_slugs({"m1"})
            task = asyncio.ensure_future(s.run())
            for _ in range(40):
                await asyncio.sleep(0.05)
                if s.messages:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return s
    s = asyncio.run(run())
    assert s.messages == 1 and s.reconnects == 0 and "bad frame" in s.last_error
