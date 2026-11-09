"""Step 4 test-order command against a fake Polymarket US exchange (no network)."""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app import test_order as T
from app.trading.us_broker import UsLiveBroker

SECRET = base64.b64encode(bytes(range(64))).decode()
NOW = time.time()
SLUG15 = None


class FakeExchange:
    """Long-side book bid 0.48 / ask 0.50. Maker orders far from the market rest; IOC orders
    fill against the book. Commission per fill: 0.095*q*p*(1-p), banker's-rounded to the cent."""

    def __init__(self, ask_qty=100.0, bid_qty=100.0, report_fill_late=False, invisible_reads=2,
                 never_appears=False):
        self.invisible_reads, self.never = invisible_reads, never_appears
        self.orders, self.n, self.calls = {}, 0, []
        self.ask_qty, self.bid_qty = ask_qty, bid_qty
        self.late = report_fill_late
        self.balance = 3.64

    def api(self, req: httpx.Request):
        self.calls.append((req.method, req.url.path))
        body = json.loads(req.content) if req.content else {}
        p = req.url.path
        if p == "/v1/account/balances":
            return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": str(self.balance)}]})
        if p == "/v1/orders/open" and req.method == "GET":
            return httpx.Response(200, json={"orders": [] if self.never else
                                             [{k: v for k, v in o.items() if not k.startswith("_")}
                                              for o in self.orders.values() if o["state"] == "ORDER_STATE_NEW"]})
        if p == "/v1/order/preview":
            return httpx.Response(200, json={"order": {"state": "ORDER_STATE_NEW", "price": body["request"]["price"]}})
        if p == "/v1/orders" and req.method == "POST":
            if body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL":
                assert body["synchronousExecution"] is True and body["maxBlockTime"] == "5"
            else:
                assert "synchronousExecution" not in body
            self.n += 1
            oid = f"o{self.n}"
            px, q = float(body["price"]["value"]), float(body["quantity"])
            o = {"id": oid, "marketSlug": body["marketSlug"], "state": "ORDER_STATE_NEW", "cumQuantity": "0",
                 "intent": body["intent"], "tif": body["tif"], "_polls": 0}
            buy = body["intent"] == "ORDER_INTENT_BUY_LONG"
            crosses = (buy and px >= 0.50) or (not buy and px <= 0.48)
            if body["participateDontInitiate"] and crosses:
                o["state"] = "ORDER_STATE_REJECTED"
            elif crosses:
                fill_px = 0.50 if buy else 0.48
                fq = min(q, self.ask_qty if buy else self.bid_qty)
                fee = round(0.095 * fq * fill_px * (1 - fill_px) + 1e-12, 2)
                o.update(_fill={"cumQuantity": str(fq), "avgPx": {"value": str(fill_px)},
                                "commissionNotionalTotalCollected": {"value": str(fee)},
                                "state": "ORDER_STATE_FILLED" if fq >= q else "ORDER_STATE_CANCELED"})
                if not self.late:
                    o.update(o.pop("_fill"))
                self.balance += (-1 if buy else 1) * fq * fill_px - fee
            self.orders[oid] = o
            return httpx.Response(200, json={"id": oid})
        if p.startswith("/v1/order/") and p.endswith("/cancel"):
            o = self.orders[p.split("/")[3]]
            if o["state"] == "ORDER_STATE_NEW":
                o["state"] = "ORDER_STATE_CANCELED"
            return httpx.Response(200, json={})
        if p.startswith("/v1/order/"):
            o = self.orders[p.split("/")[3]]
            o["_reads"] = o.get("_reads", 0) + 1
            if self.never or o["_reads"] <= self.invisible_reads:      # created asynchronously
                return httpx.Response(404, json={"code": 5, "message": "Order not found", "details": []})
            o["_polls"] += 1
            if "_fill" in o and o["_polls"] >= 2:          # fill shows up on the 2nd read
                o.update(o.pop("_fill"))
            return httpx.Response(200, json={"order": {k: v for k, v in o.items() if not k.startswith("_")}})
        return httpx.Response(404)

    def gateway(self, req: httpx.Request):
        if req.url.path.endswith("/book"):
            return httpx.Response(200, json={"marketData": {"bids": [{"px": {"value": "0.48"}, "qty": "500"}],
                                                            "offers": [{"px": {"value": "0.50"}, "qty": "500"}]}})
        return httpx.Response(200, json={"market": {"slug": req.url.path.rsplit("/", 1)[-1],
                                                    "status": "MARKET_STATUS_OPEN", "feeCoefficient": 0.095,
                                                    "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01}})


def _setup(ex, monkeypatch):
    monkeypatch.setattr(T.asyncio, "sleep", _nosleep)
    tick = [NOW_FIXED]

    def fake_time():                 # a clock that moves 50 ms per read, so timeouts still expire
        tick[0] += 0.05
        return tick[0]
    monkeypatch.setattr(T.time, "time", fake_time)
    broker = UsLiveBroker("kid", SECRET, lambda t: {"rate": 0.095, "exponent": 1, "round_cents": True},
                          client=httpx.AsyncClient(transport=httpx.MockTransport(ex.api)), base="https://api.t")
    gw = httpx.AsyncClient(transport=httpx.MockTransport(ex.gateway))
    return broker, gw


_real_sleep = asyncio.sleep


async def _nosleep(*a, **k):
    await _real_sleep(0)


NOW_FIXED = (int(NOW) // 900) * 900 + 60          # 1 minute into a 15m window


def _run(ex, monkeypatch, **kw):
    broker, gw = _setup(ex, monkeypatch)
    cfg = {"trading": {"risk": {"max_stake_usd": 1}}, "db_path": kw.pop("db")}
    return asyncio.run(T.run(cfg, broker=broker, gw=gw, prefer="15m", **kw))


def test_part1_rest_confirm_cancel(tmp_path, monkeypatch):
    ex = FakeExchange()
    rep = _run(ex, monkeypatch, fill=False, qty=None, confirm=lambda _: "y", db=str(tmp_path / "d.db"))
    assert rep.ok, rep.lines
    texts = " | ".join(t for _, t in rep.lines)
    assert "listed in your open orders" in texts and "cancel confirmed" in texts
    (o,) = ex.orders.values()
    assert o["state"] == "ORDER_STATE_CANCELED" and o["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    posts = [c for c in ex.calls if c[0] == "POST"]
    assert posts == [("POST", "/v1/orders"), ("POST", "/v1/order/o1/cancel")]


def test_part1_needs_yes(tmp_path, monkeypatch):
    ex = FakeExchange()
    with pytest.raises(T.Abort, match="nothing was placed"):
        _run(ex, monkeypatch, fill=False, qty=None, confirm=lambda _: "", db=str(tmp_path / "d.db"))
    assert not ex.orders


def test_part2_needs_exact_phrase(tmp_path, monkeypatch):
    ex = FakeExchange()
    with pytest.raises(T.Abort, match="did not match"):
        _run(ex, monkeypatch, fill=True, qty=1, confirm=lambda _: "y", input_fn=lambda _: "yes",
             db=str(tmp_path / "d.db"))
    assert len(ex.orders) == 1                                   # only the part-1 order, canceled


def test_part2_buy_sell_and_report(tmp_path, monkeypatch, capsys):
    ex = FakeExchange(report_fill_late=True)                     # fill appears on a later read
    rep = _run(ex, monkeypatch, fill=True, qty=1, confirm=lambda _: "y", input_fn=lambda _: T.FILL_PHRASE,
               db=str(tmp_path / "d.db"))
    assert rep.ok, rep.lines
    out = capsys.readouterr().out
    assert "bought 1 @ 0.5000" in out and "sold 1 @ 0.4800" in out
    assert "PART 2 RESULT" in out
    buy, sell = ex.orders["o2"], ex.orders["o3"]
    assert buy["tif"] == sell["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert sell["intent"] == "ORDER_INTENT_SELL_LONG"


def test_part2_partial_sell_retries_and_reports_leftover(tmp_path, monkeypatch, capsys):
    ex = FakeExchange(bid_qty=0.4)                               # each sell only fills 0.4
    rep = _run(ex, monkeypatch, fill=True, qty=1, confirm=lambda _: "y", input_fn=lambda _: T.FILL_PHRASE,
               db=str(tmp_path / "d.db"))
    assert rep.ok                                                # 0.4 + 0.4 + 0.2 over three tries
    sells = [o for o in ex.orders.values() if o["intent"] == "ORDER_INTENT_SELL_LONG"]
    assert len(sells) == 3


def test_part2_cost_cap(tmp_path, monkeypatch):
    ex = FakeExchange()
    with pytest.raises(T.Abort, match="above the"):
        _run(ex, monkeypatch, fill=True, qty=5, confirm=lambda _: "y", input_fn=lambda _: T.FILL_PHRASE,
             db=str(tmp_path / "d.db"))


def test_broker_waits_for_async_order_then_gives_up(monkeypatch):
    """GET answers 404 'Order not found' right after create; the broker waits instead of failing.
    An order that never shows up (and isn't in open orders) is reported rejected, not 'live' forever."""
    ex = FakeExchange(never_appears=True)
    broker, _ = _setup(ex, monkeypatch)
    o = asyncio.run(broker.place_limit(token="cpc-x:long", side="BUY", price=0.01, size=0.01, post_only=True))
    assert o.status == "live" and not o.done                      # unknown yet: treated as open
    o.created = T.time.time() - 11                                # 11 s later, still never seen
    o = asyncio.run(broker.poll(o.order_id))
    assert o.status == "rejected" and "never appeared" in o.error


def test_missing_book_tries_other_market_then_stops_cleanly(monkeypatch):
    """A new window's /book answers 404 for a while: skip it, never crash, place nothing."""
    calls = []

    def gw(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/book"):
            return httpx.Response(404, json={"code": 5, "message": "not found"})
        return FakeExchange().gateway(req)
    client = httpx.AsyncClient(transport=httpx.MockTransport(gw))
    with pytest.raises(T.Abort, match="not two-sided"):
        asyncio.run(T.pick_market(client, {}, "15m", now=NOW_FIXED))
    assert sum(p.endswith("/book") for p in calls) == 2          # tried 15m, then 5m


def test_part2_rereads_price_after_confirmation(tmp_path, monkeypatch):
    """The ask jumps 3c while the phrase is typed: nothing is bought."""
    ex = FakeExchange()
    moved = {"on": False}
    orig = ex.gateway

    def gw(req):
        if moved["on"] and req.url.path.endswith("/book"):
            return httpx.Response(200, json={"marketData": {"bids": [{"px": {"value": "0.51"}, "qty": "9"}],
                                                            "offers": [{"px": {"value": "0.53"}, "qty": "9"}]}})
        return orig(req)
    ex.gateway = gw

    def typed(_):
        moved["on"] = True
        return T.FILL_PHRASE
    with pytest.raises(T.Abort, match="moved from 0.5 to 0.53"):
        _run(ex, monkeypatch, fill=True, qty=1, confirm=lambda _: "y", input_fn=typed, db=str(tmp_path / "d.db"))
    assert all(o["intent"] != "ORDER_INTENT_BUY_LONG" or o["tif"] != "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
               for o in ex.orders.values())                        # no real buy was sent
