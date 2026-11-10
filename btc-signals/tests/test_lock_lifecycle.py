"""Profit-lock lifecycle against a simulated LIVE exchange (fills while the bot is down, cancels that take
time to confirm, cancel-all that doesn't report fills) - the review findings A-D and G."""
import asyncio
import copy
import time

import pytest

from app.trading.broker import OrderState
from app.trading.store import TradeStore
from app.trading.trader import AutoTrader
from tests.test_trading import R, TC, FakeEngine


class FakeLive:
    """Exchange state lives in self.ex; the bot only sees it through poll(), like the real API."""
    mode = "live"

    def __init__(self, engine):
        self.e = engine
        self.orders: dict[str, OrderState] = {}
        self.ex: dict[str, dict] = {}
        self.n = 0
        self.pending_cancel_reads: dict[str, int] = {}

    def _book(self, token):
        return self.e.clob.store.get(token) or {"bids": [], "asks": []}

    async def place_limit(self, *, token, side, price, size, post_only, ioc=None):
        self.n += 1
        oid = f"L{self.n}"
        o = OrderState(oid, token, side, price, size, post_only)
        bk = self._book(token)
        lv = bk["bids"] if side == "SELL" else bk["asks"]
        crosses = bool(lv) and (lv[0][0] >= price if side == "SELL" else lv[0][0] <= price)
        self.orders[oid] = o
        if post_only and crosses:
            o.status, o.error = "rejected", "post_only_would_cross"
            return o
        ex = {"state": "live", "filled": 0.0, "notional": 0.0, "fee": 0.0, "price": price, "size": size}
        immediate = (not post_only) if ioc is None else (ioc and not post_only)
        if crosses and not post_only:
            q = min(size, lv[0][1])
            ex.update(filled=q, notional=q * lv[0][0], fee=round(0.095 * q * lv[0][0] * (1 - lv[0][0]), 2))
            if q >= size - 1e-9:
                ex["state"] = "filled"
        if immediate and ex["state"] == "live":
            ex["state"] = "canceled"
        self.ex[oid] = ex
        return await self.poll(oid)

    async def poll(self, oid):
        o = self.orders[oid]
        if o.done:
            return o
        ex = self.ex[oid]
        if self.pending_cancel_reads.get(oid, 0) > 0:          # exchange still processing the cancel
            self.pending_cancel_reads[oid] -= 1
            o.filled, o.notional, o.fee = ex["filled"], ex["notional"], ex["fee"]
            o.status = "partial" if o.filled else "live"
            return o
        o.filled, o.notional, o.fee = ex["filled"], ex["notional"], ex["fee"]
        o.status = {"filled": "filled", "canceled": "canceled"}.get(ex["state"], "partial" if o.filled else "live")
        return o

    async def cancel(self, oid):
        o = self.orders[oid]
        if self.ex[oid]["state"] == "live":
            self.ex[oid]["state"] = "canceled"
        await self.poll(oid)
        if not o.done:
            o.status = "canceled"            # like UsLiveBroker: marked locally even if not confirmed
        return o

    async def cancel_all(self):
        n = 0
        for oid, ex in self.ex.items():
            if ex["state"] == "live":
                ex["state"] = "canceled"
                n += 1
        for o in self.orders.values():
            if not o.done:
                o.status = "canceled"        # no final read: fills that raced it are not reported here
        return n

    async def adopt(self, row):
        o = OrderState(row["order_id"], row["token"], row["side"], row["price"], row["size"], bool(row["post_only"]),
                       status="live", filled=row["filled"] or 0, notional=(row["filled"] or 0) * (row["avg_price"] or 0),
                       fee=row["fee"] or 0, created=row["ts"])
        self.orders[o.order_id] = o
        return await self.poll(o.order_id)

    async def balance(self):
        return 100.0

    async def heartbeat(self):
        return None

    async def close(self):
        return None


def _cfg(**exits):
    cfg = copy.deepcopy(TC)
    cfg["trading"]["exits"].update({"profit_lock_price": 0.96, "profit_lock_offset": 0.10, "take_profit_pct": 5.0,
                                    "stop_loss_pct": 5.0, **exits})
    cfg["trading"]["risk"] = {**R}
    return cfg


def _setup(tmp_path, ex=None):
    e = FakeEngine(tmp_path, int(time.time()) - 60)
    b = FakeLive(e)
    t = AutoTrader(e, _cfg(**(ex or {})), b, TradeStore(e.db, "live"))
    return e, b, t


async def _open_position(e, t, now):
    """Paper-identical flow: maker entry at 0.51 fills -> position open, lock at 0.61 resting."""
    e.book("UP", 0.50, 0.54)
    e.signal("UP", 0.70)
    await t.tick(now)
    p, = t.store.active_positions()
    size = t.broker.orders[p["entry_order"]].size
    t.broker.ex[p["entry_order"]].update(state="filled", filled=size, notional=size * 0.51)
    e.book("UP", 0.50, 0.51)
    await t.tick(now + 1)                                            # entry booked -> open
    await t.tick(now + 1.1)                                          # holding -> lock placed
    p = t.store.position(p["id"])
    assert p["status"] == "open" and p["lock_order"]
    return p


def test_lock_fill_while_bot_is_down_is_booked_on_restart(tmp_path):
    e, b, t = _setup(tmp_path)

    async def run():
        now = time.time()
        p = await _open_position(e, t, now)
        lock = p["lock_order"]
        # bot stops; while it's down the lock fills completely on the exchange
        b.ex[lock].update(state="filled", filled=p["shares"], notional=p["shares"] * 0.61)
        t2 = AutoTrader(e, _cfg(), b, t.store)                  # restart (same exchange, fresh bot memory)
        b.orders.clear()
        await t2.recover()
        p2 = t2.store.position(p["id"])
        assert p2["status"] == "closed" and p2["exit_trigger"] == "profit_lock"
        assert p2["pnl"] == pytest.approx(p["shares"] * 0.10)
        assert not [o for o in b.orders.values() if o.side == "SELL" and o.order_id != lock]   # nothing re-sold
    asyncio.run(run())


def test_partial_lock_fill_is_never_double_counted_across_restart(tmp_path):
    e, b, t = _setup(tmp_path)

    async def run():
        now = time.time()
        p = await _open_position(e, t, now)
        lock = p["lock_order"]
        b.ex[lock].update(filled=4.0, notional=4.0 * 0.61)           # 4 sold while running ...
        await t.tick(now + 1.25)
        assert t.store.position(p["id"])["exit_shares"] == pytest.approx(4.0)
        b.ex[lock].update(filled=6.0, notional=6.0 * 0.61)           # ... 2 more while the bot is down
        t2 = AutoTrader(e, _cfg(), b, t.store)
        b.orders.clear()
        await t2.recover()
        p2 = t2.store.position(p["id"])
        assert p2["exit_shares"] == pytest.approx(6.0)               # 4 + 2, not 4 + 6
        assert p2["exit_proceeds"] == pytest.approx(6.0 * 0.61)
    asyncio.run(run())


def test_bid_already_above_lock_price_sells_at_the_bid_without_spamming(tmp_path):
    e, b, t = _setup(tmp_path)

    async def run():
        now = time.time()
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        await t.tick(now)
        p, = t.store.active_positions()
        size = b.orders[p["entry_order"]].size
        b.ex[p["entry_order"]].update(state="filled", filled=size, notional=size * 0.51)
        e.book("UP", 0.63, 0.64)                                     # jumps past entry + 10c before any lock
        await t.tick(now + 1)                                        # entry booked
        await t.tick(now + 1.25)
        p = t.store.position(p["id"])
        assert p["status"] == "closed" and p["exit_trigger"] == "profit_lock"
        rejected = [o for o in b.orders.values() if o.status == "rejected"]
        assert len(rejected) <= 1                                    # no rejected order every 250 ms
    asyncio.run(run())


def test_cancel_not_confirmed_means_no_sale_until_it_is(tmp_path, monkeypatch):
    e, b, t = _setup(tmp_path, {"take_profit_pct": 0.08})
    real_sleep = asyncio.sleep

    async def fast_sleep(*a, **k):
        await real_sleep(0)
    monkeypatch.setattr("app.trading.trader.asyncio.sleep", fast_sleep)

    async def run():
        now = time.time()
        p = await _open_position(e, t, now)
        lock = p["lock_order"]
        b.pending_cancel_reads[lock] = 50                            # exchange is slow to confirm
        b.ex[lock].update(filled=3.0, notional=3.0 * 0.61)           # and the lock got a fill meanwhile
        e.book("UP", 0.57, 0.58)                                     # take-profit rule fires
        await t.tick(now + 1.25)
        p1 = t.store.position(p["id"])
        assert p1["status"] == "open" and p1["lock_order"] == lock   # not sold: lock not confirmed
        assert not [o for o in b.orders.values() if o.side == "SELL" and o.order_id != lock]
        b.pending_cancel_reads[lock] = 0                             # cancel now confirmed
        await t.tick(now + 1.5)
        p2 = t.store.position(p["id"])
        assert p2["status"] == "closed"
        sells = [o for o in b.orders.values() if o.side == "SELL" and o.order_id != lock]
        assert len(sells) == 1 and sells[0].size == pytest.approx(p["shares"] - 3.0)   # only the rest
    asyncio.run(run())


def test_no_lock_churn_while_an_exit_is_being_retried(tmp_path):
    e, b, t = _setup(tmp_path, {"take_profit_pct": 0.08})

    async def run():
        now = time.time()
        p = await _open_position(e, t, now)
        e.clob.store.apply({"asset_id": "UP", "bids": [{"price": "0.57", "size": "2"}],
                            "asks": [{"price": "0.58", "size": "100"}]})   # thin bid: partial exits
        for k in range(4):
            await t.tick(now + 1.25 + k * 0.25)
        locks = [o for o in b.orders.values() if o.purpose == "lock"]
        assert len(locks) == 1                                       # the original lock only, never re-placed
    asyncio.run(run())


def test_kill_switch_books_fills_of_an_in_flight_exit(tmp_path):
    e, b, t = _setup(tmp_path, {"take_profit_pct": 0.08})

    async def run():
        now = time.time()
        p = await _open_position(e, t, now)
        await t._cancel_lock(t.store.position(p["id"]))
        o = await b.place_limit(token="UP", side="SELL", price=0.70, size=p["shares"], post_only=True)
        b.ex[o.order_id].update(state="live")
        t.store.update_position(p["id"], status="closing", exit_order=o.order_id)
        b.ex[o.order_id].update(filled=5.0, notional=5.0 * 0.57)    # filled 5 just before the kill
        await t.kill("test")
        p2 = t.store.position(p["id"])
        assert p2["exit_shares"] == pytest.approx(5.0) and p2["status"] == "open"
    asyncio.run(run())
