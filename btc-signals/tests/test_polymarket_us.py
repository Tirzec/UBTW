"""Polymarket US adapter: slugs, market parsing, rules, books, signing, orders, gate."""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app.db import DB
from app.feeds.sources import PolymarketUSBrtiFeed, build_feed
from app.ingest.discovery import Window
from app.ingest.polymarket_ws import BookStore
from app.ingest.us_markets import UsDiscovery, apply_us_market, us_book_to_store, us_slug
from app.trading.broker import OrderState
from app.trading.live_gate import CONFIRM_PHRASE, check_live_gates, paper_record
from app.trading.store import TradeStore
from app.trading.us_broker import Signer, UsLiveBroker, apply_order_status, order_body

DESC = ("This market will settle to Up if the Bitcoin price at 11:15AM ET on Nov 9, 2026 is greater "
        "than or equal to the price at 11:10AM ET on Nov 9, 2026. Otherwise, this market will settle to "
        "Down. Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI). The price at the "
        "beginning and at the end of the time range is determined the same way: in the last minute before "
        "each of those two times, 60 BRTI prices are collected. The official value for that time is the "
        "simple average of these prices, rounded to the nearest 2 decimal places.")


def market(status="MARKET_STATUS_OPEN", ptb="75041.79", final=None, horizon="5m", index="BRTI", desc=DESC):
    """Shape copied from GET gateway.polymarket.us/v1/market/slug/cpc-btc-updown-5m-... (2026-11-09)."""
    return {"id": "2345864", "question": "BTC Up or Down 5m", "subtitle": "Nov 9, 11:10AM - 11:15AM",
            "slug": "cpc-btc-updown-5m-2026-11-09-1610z", "status": status, "description": desc,
            "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01, "feeCoefficient": 0.095,
            "assetPriceTerms": {"marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
                                "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
                                "indexSymbol": index, "horizon": horizon,
                                "priceToBeat": {"value": ptb, "currency": "USD"} if ptb else None,
                                "settlementPrice": {"value": final, "currency": "USD"} if final else None}}


def test_us_slug_format():
    assert us_slug("5m", 1794240600) == "cpc-btc-updown-5m-2026-11-09-1610z"
    assert us_slug("15m", 1794240000) == "cpc-btc-updown-15m-2026-11-09-1600z"


def test_apply_us_market_open_and_resolved():
    w = apply_us_market(Window("5m", 1794240600, 1794240900, "cpc-btc-updown-5m-2026-11-09-1610z"), market())
    assert w.found and w.accepting_orders and w.rules.ok, w.rules.mismatches
    assert w.gamma_ptb == 75041.79 and w.outcome is None and w.min_size == 0.01
    assert w.fee_schedule["rate"] == 0.095 and w.token_up.endswith(":long") and w.token_down.endswith(":short")
    w = apply_us_market(Window("5m", 0, 300, "s"), market("MARKET_STATUS_RESOLVED", "75443.43", "75247.89"))
    assert w.outcome == "DOWN" and not w.accepting_orders
    w = apply_us_market(Window("5m", 0, 300, "s"), market("MARKET_STATUS_RESOLVED", "100.00", "100.00"))
    assert w.outcome == "UP"                                          # flat window settles Up
    w = apply_us_market(Window("5m", 0, 300, "s"), market("MARKET_STATUS_CLOSED", None))
    assert not w.accepting_orders and w.outcome is None               # "closed" also means not yet open
    w = apply_us_market(Window("5m", 0, 300, "s"), market(), taker_theta_override=0.0695)
    assert w.fee_schedule["rate"] == 0.0695


def test_us_rules_mismatch_flags():
    assert not apply_us_market(Window("5m", 0, 300, "s"), market(index="ETHUSD_RTI")).rules.ok
    assert not apply_us_market(Window("5m", 0, 300, "s"), market(horizon="1h")).rules.ok
    bad = DESC.replace("simple average", "median")
    assert not apply_us_market(Window("5m", 0, 300, "s"), market(desc=bad)).rules.ok


def test_us_book_long_and_derived_short_and_trades():
    store, last = BookStore(), {}
    data = {"marketData": {"bids": [{"px": {"value": "0.6700"}, "qty": "62000"}],
                           "offers": [{"px": {"value": "0.6800"}, "qty": "47000"}],
                           "stats": {"lastTradePx": {"value": "0.6800"}, "sharesTraded": "100"}}}
    us_book_to_store(store, "S", data, 1.0, last)
    up, dn = store.get("S:long"), store.get("S:short")
    assert up["bids"][0] == (0.67, 62000) and up["asks"][0] == (0.68, 47000)
    assert dn["bids"][0] == (0.32, 47000) and dn["asks"][0] == (0.33, 62000)   # Down = 1 - Up
    assert store.trades_since("S:long", 0) == []                                # first snapshot: baseline
    data["marketData"]["stats"] = {"lastTradePx": {"value": "0.6600"}, "sharesTraded": "130"}
    us_book_to_store(store, "S", data, 2.0, last)
    assert store.trades_since("S:long", 0) == [(2.0, 0.66, 30.0)]
    assert store.trades_since("S:short", 0) == [(2.0, 0.34, 30.0)]


def test_brti_feed_parse_and_registry():
    d = {"series": [{"candles": [{"timestamp": 101, "close": "75070.24"}, {"timestamp": 100, "close": "75069"},
                                 {"timestamp": 102, "close": "75071.07"}]}]}
    ticks = PolymarketUSBrtiFeed.parse_history(d, 103.0, 100, "brti")
    assert [t.ts for t in ticks] == [101, 102] and ticks[0].kind == "spot"
    assert build_feed({"type": "polymarket_us_brti"}, "reference", {}, {}).type_name == "polymarket_us_brti"


def _keypair():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    return sk, base64.b64encode(bytes(sk) + bytes(sk.verify_key)).decode()


def test_signer_matches_documented_scheme():
    sk, secret = _keypair()
    h = Signer("KEYID", secret).headers("GET", "/v1/account/balances", ts_ms=1700000000000)
    assert h["X-PM-Access-Key"] == "KEYID" and h["X-PM-Timestamp"] == "1700000000000"
    sk.verify_key.verify(b"1700000000000GET/v1/account/balances", base64.b64decode(h["X-PM-Signature"]))


def test_order_body_converts_down_to_short_side():
    b = order_body("cpc-x:long", "BUY", 0.55, 9.09, True)
    assert b["intent"] == "ORDER_INTENT_BUY_LONG" and b["price"]["value"] == "0.55"
    assert b["participateDontInitiate"] is True and b["marketSlug"] == "cpc-x"
    assert b["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    s = order_body("cpc-x:short", "SELL", 0.30, 5, False)
    assert s["intent"] == "ORDER_INTENT_SELL_SHORT" and s["price"]["value"] == "0.70"   # long price = 1 - 0.30
    assert s["participateDontInitiate"] is False


def test_apply_order_status_converts_prices_and_reads_fees():
    o = OrderState("1", "cpc-x:short", "BUY", 0.33, 10, True)
    apply_order_status(o, {"state": "ORDER_STATE_PARTIALLY_FILLED", "cumQuantity": 4,
                           "avgPx": {"value": "0.67"}, "commissionNotionalTotalCollected": {"value": "0"}})
    assert o.status == "partial" and o.filled == 4 and o.avg_price == pytest.approx(0.33)
    apply_order_status(o, {"state": "ORDER_STATE_FILLED", "cumQuantity": 10, "avgPx": {"value": "0.67"},
                           "commissionNotionalTotalCollected": {"value": "0.21"}})
    assert o.status == "filled" and o.fee == 0.21
    r = apply_order_status(OrderState("2", "cpc-x:long", "BUY", 0.5, 10, True), {"state": "ORDER_STATE_REJECTED"})
    assert r.status == "rejected" and r.error.startswith("post_only_would_cross")


def test_live_broker_requests_are_signed_and_correct():
    sk, secret = _keypair()
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        sk.verify_key.verify(f"{req.headers['X-PM-Timestamp']}{req.method}{req.url.path}".encode(),
                             base64.b64decode(req.headers["X-PM-Signature"]))
        p = req.url.path
        if p == "/v1/orders" and req.method == "POST":
            return httpx.Response(200, json={"id": "ord-1"})
        if p == "/v1/order/ord-1":
            return httpx.Response(200, json={"order": {"state": "ORDER_STATE_NEW", "cumQuantity": 0}})
        if p == "/v1/order/ord-1/cancel":
            return httpx.Response(200, json={})
        if p == "/v1/orders/open/cancel":
            return httpx.Response(200, json={"canceledOrderIds": ["a", "b"]})
        if p == "/v1/account/balances":
            return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": 42.5}]})
        return httpx.Response(404)

    b = UsLiveBroker("KID", secret, lambda t: {}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def run():
        o = await b.place_limit(token="cpc-x:short", side="BUY", price=0.33, size=10, post_only=True)
        assert o.order_id == "ord-1" and o.status == "live"
        body = json.loads(seen[0].content)
        assert body["intent"] == "ORDER_INTENT_BUY_SHORT" and body["price"]["value"] == "0.67"
        await b.cancel("ord-1")
        assert json.loads(seen[-2].content) == {"marketSlug": "cpc-x"}
        assert await b.cancel_all() == 2 and await b.balance() == 42.5
    asyncio.run(run())


def test_live_broker_classifies_errors():
    sk, secret = _keypair()
    resp = {"code": 429}

    def handler(req):
        if resp["code"] == 429:
            return httpx.Response(429, json={"status": 429})
        if resp["code"] == 400:
            return httpx.Response(400, text="Order Processing Max Delay Exceeded")
        return httpx.Response(401, text="unauthorized")

    from app.trading.broker import BrokerError, Maintenance
    b = UsLiveBroker("KID", secret, lambda t: {}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(Maintenance):
        asyncio.run(b.balance())
    resp["code"] = 400
    with pytest.raises(Maintenance):
        asyncio.run(b.balance())
    resp["code"] = 401
    with pytest.raises(BrokerError, match="auth failed"):
        asyncio.run(b.balance())


def test_paper_record_is_per_venue_and_gate_us(tmp_path, monkeypatch):
    db = tmp_path / "v.db"
    d = DB(db)
    TradeStore(d, "paper", "polymarket_us")
    for i in range(300):
        d.insert("bot_positions", {"mode": "paper", "venue": "polymarket_intl", "status": "closed", "pnl": 0.1,
                                   "cost": 5, "entry_fee": 0, "exit_fee": 0, "closed_ts": i})
    assert paper_record(str(db), "polymarket_us")[0] == 0           # intl practice doesn't count for US
    assert paper_record(str(db), "polymarket_intl")[0] == 300
    cfg = {"venue": "polymarket_us", "trading": {"live_trading_enabled": True, "min_paper_trades_for_live": 300}}
    monkeypatch.setenv("POLYMARKET_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", "s" * 44)
    with pytest.raises(SystemExit, match="0 paper trades completed on polymarket_us"):
        check_live_gates(cfg, str(db), input_fn=lambda _: CONFIRM_PHRASE, account_fn=lambda *a: (True, "ok"))
    for i in range(300):
        d.insert("bot_positions", {"mode": "paper", "venue": "polymarket_us", "status": "closed", "pnl": 0.1,
                                   "cost": 5, "entry_fee": 0, "exit_fee": 0, "closed_ts": i})
    with pytest.raises(SystemExit, match="API key check failed"):
        check_live_gates(cfg, str(db), input_fn=lambda _: CONFIRM_PHRASE, account_fn=lambda *a: (False, "HTTP 401"))
    with pytest.raises(SystemExit, match="did not match"):
        check_live_gates(cfg, str(db), input_fn=lambda _: "ok", account_fn=lambda *a: (True, "ok"))
    creds = check_live_gates(cfg, str(db), input_fn=lambda _: CONFIRM_PHRASE, account_fn=lambda *a: (True, "$50"))
    assert creds == {"key_id": "kid", "secret_key": "s" * 44}


def test_us_discovery_refresh_uses_gateway():
    def handler(req):
        assert req.url.path == "/v1/market/slug/cpc-btc-updown-5m-2026-11-09-1610z"
        return httpx.Response(200, json={"market": market()})
    disc = UsDiscovery({"polymarket_us": {}}, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    w = disc.get("5m", 1794240600)
    asyncio.run(disc.refresh(w))
    assert w.found and w.price_to_beat is None and w.gamma_ptb == 75041.79 and w.venue == "polymarket_us"


def test_sports_paper_trades_do_not_count_toward_crypto_gate(tmp_path):
    from app.db import DB
    from app.trading.live_gate import paper_record
    from app.trading.store import TradeStore
    db = tmp_path / "m.db"
    d = DB(db)
    TradeStore(d, "paper", "polymarket_us", "sports")
    for i in range(250):
        d.insert("bot_positions", {"mode": "paper", "status": "closed", "pnl": 0.1, "cost": 5, "entry_fee": 0,
                                   "exit_fee": 0, "closed_ts": i, "venue": "polymarket_us", "module": "sports"})
    assert paper_record(str(db), "polymarket_us")[0] == 0
    assert paper_record(str(db), "polymarket_us", "sports")[0] == 250
