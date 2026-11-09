"""Order execution. PaperBroker simulates against the live order book; LiveBroker
uses the official Polymarket SDK (polymarket-client, Python 3.11+).

Both expose the same async interface, so the trader logic is identical in both modes.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..fees import fee_usdc

log = logging.getLogger("trader")


@dataclass
class OrderState:
    order_id: str
    token: str
    side: str                 # BUY | SELL
    price: float              # limit price
    size: float               # requested shares
    post_only: bool
    status: str = "live"      # live | filled | partial | canceled | rejected
    filled: float = 0.0
    notional: float = 0.0     # sum(fill price * shares)
    fee: float = 0.0
    error: str = ""
    created: float = field(default_factory=lambda: time.time())   # looked up per call (testable clock)

    @property
    def avg_price(self) -> float:
        return self.notional / self.filled if self.filled else 0.0

    @property
    def done(self) -> bool:
        return self.status in ("filled", "canceled", "rejected")


class BrokerError(Exception):
    """API/transport failure (counts toward the API-error pause)."""


class Maintenance(Exception):
    """Matching engine restart / post-only mode: pause new orders until retry_after."""

    def __init__(self, msg: str, retry_after: float = 60):
        super().__init__(msg)
        self.retry_after = retry_after


# ------------------------------------------------------------------------- paper
class PaperBroker:
    """Simulated fills against the real Polymarket order book.

    BUY limit at P:
      * marketable part (non post-only): walks asks <= P now, taker fee charged
      * resting part fills (maker, fee 0) once the best ask falls to <= P or a trade
        prints strictly below P after the order was placed (conservative about queue)
    SELL is symmetric against bids. A post-only order that would cross is rejected,
    exactly like the real CLOB ("post_only_would_cross").
    """

    mode = "paper"

    def __init__(self, get_book: Callable[[str], Optional[dict]],
                 get_trades: Callable[[str, float], list], fee_schedule_for: Callable[[str], dict],
                 balance_usd: float = 1000.0, ioc_non_post_only: bool = False):
        self.get_book = get_book
        self.get_trades = get_trades
        self.fee_schedule_for = fee_schedule_for
        self.orders: dict[str, OrderState] = {}
        self.balance_usd = balance_usd
        self.ioc_non_post_only = ioc_non_post_only

    async def place_limit(self, *, token: str, side: str, price: float, size: float,
                          post_only: bool, ioc: bool | None = None) -> OrderState:
        o = OrderState(f"paper-{uuid.uuid4().hex[:12]}", token, side, price, size, post_only)
        book = self.get_book(token) or {"bids": [], "asks": []}
        opp = book["asks"] if side == "BUY" else book["bids"]
        crosses = [(p, s) for p, s in opp if (p <= price + 1e-9 if side == "BUY" else p >= price - 1e-9)]
        if post_only and crosses:
            o.status, o.error = "rejected", "post_only_would_cross"
            self.orders[o.order_id] = o
            return o
        if not post_only:
            sched = self.fee_schedule_for(token)
            for p, s in crosses:
                take = min(s, o.size - o.filled)
                if take <= 0:
                    break
                o.filled += take
                o.notional += take * p
                o.fee += fee_usdc(take, p, sched)
        if o.filled >= o.size - 1e-9:
            o.status = "filled"
        elif not post_only and (ioc if ioc is not None else self.ioc_non_post_only):
            o.status = "canceled"          # immediate-or-cancel: the unfilled rest never rests
        else:
            o.status = "partial" if o.filled else "live"
        self.orders[o.order_id] = o
        return o

    async def poll(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o.done:
            return o
        book = self.get_book(o.token) or {"bids": [], "asks": []}
        rest = o.size - o.filled
        hit = False
        if o.side == "BUY":
            hit = bool(book["asks"]) and book["asks"][0][0] <= o.price + 1e-9
            hit = hit or any(p < o.price - 1e-9 for _, p, _ in self.get_trades(o.token, o.created))
        else:
            hit = bool(book["bids"]) and book["bids"][0][0] >= o.price - 1e-9
            hit = hit or any(p > o.price + 1e-9 for _, p, _ in self.get_trades(o.token, o.created))
        if hit and rest > 0:
            o.filled += rest
            o.notional += rest * o.price      # resting fill at our limit: maker, no fee
            o.status = "filled"
        return o

    async def cancel(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if not o.done:
            o.status = "canceled"
        return o

    async def cancel_all(self) -> int:
        n = 0
        for o in self.orders.values():
            if not o.done:
                o.status = "canceled"
                n += 1
        return n

    async def balance(self) -> float | None:
        return self.balance_usd

    async def adopt(self, row: dict) -> OrderState:
        """Restore an order from the audit log after a restart (simulated orders don't survive)."""
        o = OrderState(row["order_id"], row["token"], row["side"], row["price"], row["size"],
                       bool(row["post_only"]), status="canceled", filled=row["filled"] or 0,
                       notional=(row["filled"] or 0) * (row["avg_price"] or 0), fee=row["fee"] or 0,
                       created=row["ts"])
        self.orders[o.order_id] = o
        return o

    async def heartbeat(self) -> None:
        return None

    async def close(self) -> None:
        return None


# ------------------------------------------------------------------------- live
class LiveBroker:
    """Real orders via the official SDK. Never constructed unless every LIVE gate passed."""

    mode = "live"

    def __init__(self, client, fee_schedule_for: Callable[[str], dict]):
        self.client = client
        self.fee_schedule_for = fee_schedule_for
        self.orders: dict[str, OrderState] = {}
        self._hb_id = ""

    @classmethod
    async def connect(cls, private_key: str, wallet: str, fee_schedule_for) -> "LiveBroker":
        if sys.version_info < (3, 11):
            raise BrokerError("LIVE mode needs Python 3.11+ (the official Polymarket SDK requires it)")
        try:
            from polymarket import AsyncSecureClient  # type: ignore
        except ImportError as e:
            raise BrokerError('LIVE mode needs the official SDK: pip install "polymarket-client>=0.12.0"') from e
        client = await AsyncSecureClient.create(private_key=private_key, wallet=wallet or None)
        return cls(client, fee_schedule_for)

    @staticmethod
    def _classify(e: Exception) -> Exception:
        name = type(e).__name__
        msg = str(e)
        retry = getattr(e, "retry_after", None)
        if "post_only_mode" in msg or "425" in msg or getattr(e, "restriction", None):
            return Maintenance(msg, float(retry or 60))
        return BrokerError(f"{name}: {msg}"[:300])

    async def place_limit(self, *, token: str, side: str, price: float, size: float,
                          post_only: bool, ioc: bool | None = None) -> OrderState:
        o = OrderState("", token, side, price, size, post_only)
        try:
            r = await self.client.place_limit_order(asset_id=token, side=side, price=f"{price:.2f}",
                                                    size=f"{size:.2f}", post_only=post_only)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e) from e
        if not getattr(r, "ok", False):
            code = getattr(r, "code", "unknown")
            o.order_id = f"rejected-{uuid.uuid4().hex[:8]}"
            o.status, o.error = "rejected", f"{code}: {getattr(r, 'message', '')}"[:300]
            if code == "post_only_mode":
                raise Maintenance(o.error, 120)
            self.orders[o.order_id] = o
            return o
        o.order_id = r.order_id
        if getattr(r, "status", "") == "matched":
            # crossed immediately: taker fill. Exact size comes from the next poll.
            o.status = "partial"
        self.orders[o.order_id] = o
        return await self.poll(o.order_id)

    async def poll(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        if o.done:
            return o
        try:
            oo = await self.client.get_order(order_id=order_id)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e) from e
        matched = float(oo.size_matched or 0)
        if matched > o.filled + 1e-9:
            new = matched - o.filled
            px = float(oo.price)
            o.notional += new * px
            # crossed at placement -> taker (fee); later fills of a resting order -> maker (0)
            taker = (not o.post_only) and time.time() - o.created < 2.0
            o.fee += fee_usdc(new, px, self.fee_schedule_for(o.token)) if taker else 0.0
            o.filled = matched
        st = (oo.status or "").upper()
        if o.filled >= o.size - 1e-9 or st == "MATCHED":
            o.status = "filled"
        elif st in ("CANCELED", "CANCELLED", "INVALID", "EXPIRED"):
            o.status = "canceled"
        else:
            o.status = "partial" if o.filled else "live"
        return o

    async def cancel(self, order_id: str) -> OrderState:
        o = self.orders[order_id]
        try:
            await self.client.cancel_order(order_id=order_id)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e) from e
        await self.poll(order_id)          # capture any fill that raced the cancel
        if not o.done:
            o.status = "canceled"
        return o

    async def cancel_all(self) -> int:
        try:
            r = await self.client.cancel_all()
        except Exception as e:  # noqa: BLE001
            raise self._classify(e) from e
        for o in self.orders.values():
            if not o.done:
                o.status = "canceled"
        return len(getattr(r, "canceled", []) or [])

    async def adopt(self, row: dict) -> OrderState:
        """Restore an order from the audit log after a restart and refresh it from the exchange."""
        o = OrderState(row["order_id"], row["token"], row["side"], row["price"], row["size"],
                       bool(row["post_only"]), status="live", filled=row["filled"] or 0,
                       notional=(row["filled"] or 0) * (row["avg_price"] or 0), fee=row["fee"] or 0,
                       created=row["ts"])
        self.orders[o.order_id] = o
        return await self.poll(o.order_id)

    async def balance(self) -> float | None:
        try:
            r = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        except Exception as e:  # noqa: BLE001
            raise self._classify(e) from e
        return float(r.balance) / 1e6          # base units, 6 decimals

    async def heartbeat(self) -> None:
        """Keep the exchange's dead-man switch armed: if we stop for 10s it cancels our orders."""
        from polymarket import OrderHeartbeatMismatchError  # type: ignore
        try:
            hb = await self.client.send_order_heartbeat(heartbeat_id=self._hb_id)
        except OrderHeartbeatMismatchError as e:
            hb = await self.client.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
        self._hb_id = hb.heartbeat_id

    async def redeem(self, condition_id: str) -> None:
        await self.client.redeem_positions(condition_id=condition_id)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.client.close(), 5)
        except Exception:  # noqa: BLE001
            pass
