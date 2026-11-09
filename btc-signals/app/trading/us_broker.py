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

import base64
import json
import time
import uuid
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


def order_body(token: str, side: str, price: float, size: float, post_only: bool) -> dict:
    """Build the create-order request (pure; unit-tested)."""
    slug, leg = split_token(token)
    long_px = price if leg == "long" else 1.0 - price
    return {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{round(long_px, 2):.2f}", "currency": "USD"},
            "quantity": round(size, 2), "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "intent": INTENTS[(leg, side)], "participateDontInitiate": bool(post_only),
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}


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

    def __init__(self, key_id: str, secret: str, fee_schedule_for: Callable[[str], dict],
                 client: httpx.AsyncClient | None = None, base: str = API):
        self.signer = Signer(key_id, secret)
        self.fee_schedule_for = fee_schedule_for
        self.client = client or httpx.AsyncClient(timeout=10)
        self.base = base
        self.orders: dict[str, OrderState] = {}

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
                          post_only: bool) -> OrderState:
        o = OrderState("", token, side, price, size, post_only)
        try:
            d = await self._req("POST", "/v1/orders", order_body(token, side, price, size, post_only))
        except BrokerError as e:
            if post_only and ("participate" in str(e).lower() or "would match" in str(e).lower()):
                o.order_id, o.status = f"rejected-{uuid.uuid4().hex[:8]}", "rejected"
                o.error = f"post_only_would_cross: {e}"
                self.orders[o.order_id] = o
                return o
            raise
        o.order_id = str(d.get("id") or "")
        if not o.order_id:
            raise BrokerError(f"no order id in response: {d}")
        self.orders[o.order_id] = o
        return await self.poll(o.order_id)

    async def poll(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o.done:
            return o
        d = await self._req("GET", f"/v1/order/{order_id}")
        return apply_order_status(o, d.get("order") or d)

    async def cancel(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
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
