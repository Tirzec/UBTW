"""LIVE orders on Polymarket US through its documented Retail REST API.

Auth (docs.polymarket.us/api-reference/authentication): headers X-PM-Access-Key,
X-PM-Timestamp (ms) and X-PM-Signature = base64(Ed25519_sign(timestamp + METHOD + path)),
signed with the first 32 bytes of the base64 secret key. Signed directly with PyNaCl so it
runs on Python 3.9 (the official polymarket-us SDK needs 3.10+).

Orders: POST /v1/orders {marketSlug, type LIMIT, price{value,currency}, quantity, tif,
intent, participateDontInitiate (maker-only), manualOrderIndicator AUTOMATIC}. price.value
is ALWAYS the long (Up) price: a Down order at x is sent as BUY/SELL_SHORT at 1 - x.
Fees are read from the exchange's own commissionNotionalTotalCollected on each order.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
import weakref
from typing import Callable

import httpx

from .broker import BrokerError, Maintenance, OrderState

API = "https://api.polymarket.us"

INTENTS = {("long", "BUY"): "ORDER_INTENT_BUY_LONG", ("long", "SELL"): "ORDER_INTENT_SELL_LONG",
           ("short", "BUY"): "ORDER_INTENT_BUY_SHORT", ("short", "SELL"): "ORDER_INTENT_SELL_SHORT"}
DONE_CANCELED = {"ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED", "ORDER_STATE_REPLACED"}


class Signer:
    def __init__(self, key_id: str, secret_b64: str):
        from nacl.signing import SigningKey  # PyNaCl

        self.key_id = key_id
        self._key = SigningKey(base64.b64decode(secret_b64)[:32])

    def headers(self, method: str, path: str, ts_ms: int | None = None) -> dict:
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        sig = self._key.sign(f"{ts}{method.upper()}{path}".encode()).signature
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode(), "Content-Type": "application/json"}


def split_token(token: str) -> tuple[str, str]:
    slug, side = token.rsplit(":", 1)
    if side not in ("long", "short"):
        raise ValueError(f"bad Polymarket US token {token!r}")
    return slug, side


def tick_decimals(tick: float) -> int:
    s = f"{tick:.6f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def format_price(px: float, tick: float) -> str:
    """Snap to the market's tick (0.01; NFL uses 0.0025 / 0.005) and format exactly."""
    snapped = round(round(px / tick) * tick, 6)
    snapped = min(max(snapped, 0.01), 0.99)            # exchange absolute limits
    return f"{snapped:.{max(2, tick_decimals(tick))}f}"


def order_body(token: str, side: str, price: float, size: float, post_only: bool,
               tick: float = 0.01, ioc: bool | None = None) -> dict:
    """Build the create-order request (pure; unit-tested).

    Entries (post_only) rest as GTC maker-only orders. Non-post-only orders (exits, crossing
    entries) are IMMEDIATE-OR-CANCEL: they never rest, so a lost response can't leave a
    second live order behind."""
    slug, leg = split_token(token)
    long_px = price if leg == "long" else 1.0 - price
    immediate = (not post_only) if ioc is None else (bool(ioc) and not post_only)
    body = {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
            "price": {"value": format_price(long_px, tick), "currency": "USD"},
            "quantity": round(size, 2),
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" if immediate else "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "intent": INTENTS[(leg, side)], "participateDontInitiate": bool(post_only),
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
    if immediate:
        # docs (orders overview, best practices): use synchronousExecution only for
        # immediately-fillable orders, to wait for the fill confirmation; resting orders stay async
        body["synchronousExecution"] = True
        body["maxBlockTime"] = "5"
    return body


def outcome_unknown(e: Exception) -> bool:
    """A failure where the order may or may not have been created (timeout, dropped connection,
    server error) - as opposed to a clean rejection."""
    m = str(e)
    if "HTTP 5" in m:
        return True
    return any(k in m for k in ("Timeout", "RemoteProtocolError", "ReadError", "WriteError",
                                "ConnectionReset", "NetworkError", "Disconnected"))


def _ts_iso(s) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_trades(d: dict, slug: str | None = None, since: float = 0.0) -> list[dict]:
    """TRADE activities -> [{id, slug, price, qty, aggressor, ts, state}], newest first."""
    out = []
    for a in (d or {}).get("activities") or []:
        t = a.get("trade") if isinstance(a, dict) else None
        if not t:
            continue
        if slug and t.get("marketSlug") != slug:
            continue
        ts = _ts_iso(t.get("createTime"))
        if ts < since:
            continue
        if t.get("state") in ("TRADE_STATE_BUSTED", "TRADE_STATE_REJECTED"):
            continue
        out.append({"id": str(t.get("id") or ""), "slug": t.get("marketSlug"),
                    "price": _amt(t.get("price")) or 0.0,
                    "qty": float(t.get("qtyDecimal") or t.get("qty") or 0),
                    "aggressor": bool(t.get("isAggressor", True)), "ts": ts, "state": t.get("state", ""),
                    "cost_basis": _amt(t.get("costBasis")), "realized": _amt(t.get("realizedPnl"))})
    return out


def _amt(x) -> float | None:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def apply_order_status(o: OrderState, order: dict) -> OrderState:
    """Update OrderState from a GET /v1/order response (prices converted to our side)."""
    _, leg = split_token(o.token)
    filled = float(order.get("cumQuantity") or 0)
    avg_long = _amt(order.get("avgPx"))
    if filled > 0 and avg_long is not None:
        avg = avg_long if leg == "long" else 1.0 - avg_long
        o.filled, o.notional = filled, filled * avg
    fee = _amt(order.get("commissionNotionalTotalCollected"))
    if fee is not None:
        o.fee = abs(fee)
    st = order.get("state", "")
    if st == "ORDER_STATE_FILLED" or (o.filled >= o.size - 1e-9 and o.filled > 0):
        o.status = "filled"
    elif st == "ORDER_STATE_REJECTED":
        o.status = "rejected"
        o.error = o.error or ("post_only_would_cross (maker-only order rejected)" if o.post_only
                              else "rejected by exchange")
    elif st in DONE_CANCELED:
        o.status = "canceled"
    else:
        o.status = "partial" if o.filled else "live"
    return o


class UsLiveBroker:
    mode = "live"
    # every live broker on this account (one per module): lost-order matching must see all their orders
    _instances: "weakref.WeakSet" = weakref.WeakSet()

    def __init__(self, key_id: str, secret: str, fee_schedule_for: Callable[[str], dict],
                 client: httpx.AsyncClient | None = None, base: str = API,
                 tick_for: Callable[[str], float] | None = None):
        self.signer = Signer(key_id, secret)
        self.fee_schedule_for = fee_schedule_for
        self.tick_for = tick_for or (lambda token: 0.01)
        self.client = client or httpx.AsyncClient(timeout=15)     # > maxBlockTime of sync orders
        self.last_create: dict = {}
        self.base = base
        self.orders: dict[str, OrderState] = {}
        # Orders are created asynchronously: right after POST /v1/orders, GET /v1/order/{id} can
        # answer 404 "Order not found" for a moment. Track which orders the exchange has shown us.
        self._seen: set[str] = set()
        self._known_trades: set[str] = set()
        self.visibility_timeout_s = 10.0
        self.visibility_poll_s = 0.4
        UsLiveBroker._instances.add(self)

    async def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = await self.client.request(method, self.base + path, headers=self.signer.headers(method, path),
                                          content=json.dumps(body) if body is not None else None)
        except httpx.HTTPError as e:
            raise BrokerError(f"{type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise Maintenance("rate limited (429)", 2)
        if r.status_code >= 400:
            msg = r.text[:300]
            if "Max Delay Exceeded" in msg or "Global Rate Limit" in msg:
                raise Maintenance(f"exchange latency stopgap: {msg}", 2)   # documented transient reject
            if r.status_code in (401, 403):
                raise BrokerError(f"auth failed ({r.status_code}): check POLYMARKET_KEY_ID/SECRET_KEY")
            raise BrokerError(f"HTTP {r.status_code}: {msg}")
        return r.json() if r.content else {}

    async def place_limit(self, *, token: str, side: str, price: float, size: float,
                          post_only: bool, ioc: bool | None = None) -> OrderState:
        """post_only: maker-only GTC. Otherwise IOC (default) or, with ioc=False, a GTC limit order
        that may cross and rests if it doesn't fill (the caller cancels it after a timeout)."""
        o = OrderState("", token, side, price, size, post_only)
        body = order_body(token, side, price, size, post_only, self.tick_for(token), ioc)
        t0 = time.time()
        try:
            d = await self._req("POST", "/v1/orders", body)
        except BrokerError as e:
            if post_only and ("participate" in str(e).lower() or "would match" in str(e).lower()):
                o.order_id, o.status = f"rejected-{uuid.uuid4().hex[:8]}", "rejected"
                o.error = f"post_only_would_cross: {e}"
                self.orders[o.order_id] = o
                return o
            if outcome_unknown(e):
                # The request may have reached the exchange. NEVER blindly re-send: look for it first.
                found = await self.find_lost_order(o, body, t0)
                if found is not None:
                    return found
                raise BrokerError(f"order outcome unknown after '{e}'; not found on the exchange, "
                                  "so it was not re-sent (reconciliation keeps watching)") from e
            raise
        self.last_create = d if isinstance(d, dict) else {}
        o.order_id = str(d.get("id") or d.get("orderId") or "")
        if not o.order_id:
            raise BrokerError(f"no order id in response: {d}")
        self.orders[o.order_id] = o
        try:
            return await self.wait_visible(o.order_id, 3.0)
        except (BrokerError, Maintenance):
            return o          # the order EXISTS: hand it back (not done) so the caller follows / cancels it

    async def find_lost_order(self, o: OrderState, body: dict, t0: float, settle_s: float = 2.0,
                              tries: int = 3) -> OrderState | None:
        """After a network failure on POST /v1/orders: was the order created anyway?
        Resting orders: an open order on the same market/intent/price/quantity we don't know yet.
        Immediate orders: new trades on that market since we sent it (GET /v1/portfolio/activities).
        The exchange has no client order ID, so this matching is the duplicate guard."""
        slug = body["marketSlug"]
        ambiguous = False
        for i in range(tries):
            await asyncio.sleep(settle_s if i == 0 else 1.0)
            try:
                d = await self._req("GET", "/v1/orders/open")
                items = d.get("orders") if isinstance(d, dict) else d
                for x in items or []:
                    oid = str(x.get("id") or x.get("orderId") or "")
                    px = x.get("price", {})
                    px = px.get("value") if isinstance(px, dict) else px
                    if (oid and oid not in self.orders and x.get("marketSlug") == slug
                            and x.get("intent", body["intent"]) == body["intent"]
                            and abs(float(px or -1) - float(body["price"]["value"])) < 1e-9
                            and abs(float(x.get("quantity") or 0) - float(body["quantity"])) < 1e-9):
                        o.order_id = oid
                        self.orders[oid] = o
                        self._seen.add(oid)
                        return await self.poll(oid)
                if not o.post_only:
                    # Another of our orders working on this market around t0 (e.g. a stacked copy's resting
                    # buy) makes trade history ambiguous: never book its fills as this order's. Leave the
                    # outcome unknown instead (the caller does not re-send; reconciliation watches).
                    if any(x is not o and x.token.rsplit(":", 1)[0] == slug and (not x.done or x.created >= t0 - 30)
                           for b in list(UsLiveBroker._instances) for x in list(b.orders.values())):
                        ambiguous = True               # keep re-scanning the open orders, skip trade history
                        continue
                    trades = await self.recent_trades(slug, since=t0 - 1.0)
                    plong = float(body["price"]["value"])
                    buys_long = body["intent"] in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT")
                    new = [t for t in trades if t["id"] not in self._known_trades and t.get("aggressor", True)
                           and (t["price"] <= plong + 1e-9 if buys_long else t["price"] >= plong - 1e-9)]
                    if sum(t["qty"] for t in new) > float(body["quantity"]) + 1e-9:
                        ambiguous = True                   # more than this order could have filled
                        continue
                    if new:
                        self._known_trades.update(t["id"] for t in new)
                        qty = sum(t["qty"] for t in new)
                        _, leg = split_token(o.token)
                        notional = sum(t["qty"] * (t["price"] if leg == "long" else 1 - t["price"]) for t in new)
                        o.order_id = "recovered-" + new[0]["id"]
                        o.filled, o.notional = min(qty, o.size), notional
                        from ..fees import fee_usdc
                        sched = self.fee_schedule_for(o.token)
                        o.fee = sum(fee_usdc(t["qty"], t["price"], sched) for t in new if t.get("aggressor", True))
                        o.status = "filled" if o.filled >= o.size - 1e-9 else "canceled"
                        o.error = "recovered from trade history after a network error (fee estimated)"
                        self.orders[o.order_id] = o
                        return o
            except (BrokerError, Maintenance):
                continue
        return None

    async def recent_trades(self, slug: str | None = None, since: float = 0.0) -> list[dict]:
        """Your own fills from GET /v1/portfolio/activities (newest 100), parsed."""
        d = await self._req("GET", "/v1/portfolio/activities")
        return parse_trades(d, slug, since)

    async def preview(self, *, token: str, side: str, price: float, size: float, post_only: bool) -> dict:
        """POST /v1/order/preview: the exchange validates the order and shows expected values.
        Places nothing."""
        body = order_body(token, side, price, size, post_only, self.tick_for(token))
        body.pop("synchronousExecution", None)
        body.pop("maxBlockTime", None)
        d = await self._req("POST", "/v1/order/preview", {"request": body})
        return d.get("order") or d

    async def wait_visible(self, order_id: str, timeout: float) -> OrderState:
        """Poll until the exchange shows the order (or it is finished), up to `timeout` s."""
        o = await self.poll(order_id)
        deadline = time.time() + timeout
        while order_id not in self._seen and not o.done and time.time() < deadline:
            await asyncio.sleep(self.visibility_poll_s)
            o = await self.poll(order_id)
        return o

    async def poll(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o.done:
            return o
        try:
            d = await self._req("GET", f"/v1/order/{order_id}")
        except BrokerError as e:
            if "404" not in str(e) and "not found" not in str(e).lower():
                raise
            if order_id in self._seen or time.time() - o.created < self.visibility_timeout_s:
                return o                          # not visible yet: still treated as live (not done)
            # never appeared: only call it gone if it's also absent from the open-orders list
            if order_id in await self.open_order_ids():
                self._seen.add(order_id)
                return o
            o.status = "rejected"
            o.error = o.error or "order never appeared on the exchange (not accepted)"
            return o
        self._seen.add(order_id)
        return apply_order_status(o, d.get("order") or d)

    async def open_order_ids(self) -> set:
        d = await self._req("GET", "/v1/orders/open")
        items = d.get("orders") if isinstance(d, dict) else d
        if not isinstance(items, list):
            items = next((v for v in d.values() if isinstance(v, list)), []) if isinstance(d, dict) else []
        return {str(x.get("id") or x.get("orderId") or "") for x in items}

    async def cancel(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if order_id not in self._seen and not o.done:
            o = await self.wait_visible(order_id, self.visibility_timeout_s)   # can't cancel what isn't there yet
            if o.done:
                return o
        slug, _ = split_token(o.token)
        try:
            await self._req("POST", f"/v1/order/{order_id}/cancel", {"marketSlug": slug})
        except BrokerError as e:
            if "404" not in str(e):
                raise
        await self.poll(order_id)          # capture fills that raced the cancel
        if not o.done:
            o.status = "canceled"
        return o

    async def cancel_all(self) -> int:
        d = await self._req("POST", "/v1/orders/open/cancel", {"slugs": []})
        for o in self.orders.values():
            if not o.done:
                o.status = "canceled"
        return len(d.get("canceledOrderIds") or [])

    async def balance(self) -> float | None:
        d = await self._req("GET", "/v1/account/balances")
        for b in d.get("balances") or []:
            if (b.get("currency") or "USD") == "USD":
                return float(b.get("buyingPower") or b.get("currentBalance") or 0)
        return None

    async def adopt(self, row: dict) -> OrderState:
        o = OrderState(row["order_id"], row["token"], row["side"], row["price"], row["size"],
                       bool(row["post_only"]), status="live", filled=row["filled"] or 0,
                       notional=(row["filled"] or 0) * (row["avg_price"] or 0), fee=row["fee"] or 0,
                       created=row["ts"])
        self.orders[o.order_id] = o
        return await self.poll(o.order_id)

    async def heartbeat(self) -> None:
        return None          # Polymarket US has no order dead-man switch; startup cancel_all covers restarts

    async def close(self) -> None:
        await self.client.aclose()
