"""Reconciliation: the bot's records vs. the exchange, every 60 s in LIVE mode (and at startup).

Compared:
  positions    GET /v1/portfolio/positions -> netPositionDecimal per market. A long (Up/Yes)
               position is positive; a short (Down/No) position is negative.
  open orders  GET /v1/orders/open vs. the orders the bot believes are open.
A market with an order in flight (position opening/closing) is skipped for that check.
Any mismatch seen on two checks in a row (or once at startup) HALTS all trading - every open
order canceled, no new entries - and sends an alert. Positions are never touched.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("trader")
TOL = 1e-6


@dataclass
class ReconResult:
    ok: bool
    mismatches: list = field(default_factory=list)
    exchange_positions: dict = field(default_factory=dict)
    bot_positions: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)
    ts: float = field(default_factory=time.time)


def _f(x) -> float:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_positions(d: dict) -> dict:
    """{market slug: net quantity} from GET /v1/portfolio/positions (expired / zero ignored)."""
    out = {}
    pos = (d or {}).get("positions") or {}
    items = pos.items() if isinstance(pos, dict) else [(p.get("marketSlug") or (p.get("marketMetadata") or {}).get("slug"), p)
                                                      for p in pos]
    for slug, p in items:
        if not slug or not isinstance(p, dict) or p.get("expired"):
            continue
        q = p.get("netPositionDecimal", p.get("netPosition"))
        q = _f(q)
        if abs(q) > TOL:
            out[slug] = q
    return out


def parse_expired(d: dict) -> dict:
    """{slug: net quantity} of positions GET /v1/portfolio/positions reports as expired (settling/settled)."""
    out = {}
    pos = (d or {}).get("positions") or {}
    items = pos.items() if isinstance(pos, dict) else [(p.get("marketSlug") or (p.get("marketMetadata") or {}).get("slug"), p)
                                                      for p in pos]
    for slug, p in items:
        if slug and isinstance(p, dict) and p.get("expired"):
            out[slug] = _f(p.get("netPositionDecimal", p.get("netPosition")))
    return out


def bot_expected(db, venue: str = "polymarket_us") -> tuple[dict, set]:
    """(expected net per slug, slugs with an order in flight)."""
    rows = db.query("SELECT * FROM bot_positions WHERE mode='live' AND COALESCE(venue,'polymarket_intl')=? "
                    "AND COALESCE(module,'crypto_updown')!='test_order' "
                    "AND status IN ('opening','open','closing','resolving')", (venue,))
    expected: dict = {}
    inflight: set = set()
    for p in rows:
        slug = p["slug"]
        if p["status"] in ("opening", "closing"):
            inflight.add(slug)
            continue
        if p["status"] == "resolving":
            inflight.add(slug)                      # settling: the exchange may already show it expired
            continue
        held = (p["shares"] or 0) - (p["exit_shares"] or 0)
        sign = -1.0 if str(p["token"]).endswith(":short") else 1.0
        expected[slug] = expected.get(slug, 0.0) + sign * held
    return {k: v for k, v in expected.items() if abs(v) > TOL}, inflight


def compare(expected: dict, exchange: dict, inflight: set, bot_open: set | None, exch_open: list) -> list[str]:
    """bot_open None = don't compare orders (startup: leftover orders are canceled anyway)."""
    out = []
    for slug in sorted(set(expected) | set(exchange)):
        if slug in inflight:
            continue
        a, b = expected.get(slug, 0.0), exchange.get(slug, 0.0)
        if abs(a - b) > 0.005:
            if not a:
                out.append(f"{slug}: exchange shows a position of {b:+g} the bot didn't open")
            elif not b:
                out.append(f"{slug}: bot thinks it holds {a:+g}, exchange shows none")
            else:
                out.append(f"{slug}: bot thinks {a:+g}, exchange shows {b:+g}")
    if bot_open is None:
        return out
    exch_ids = {str(o.get("id") or o.get("orderId") or "") for o in exch_open}
    for o in exch_open:
        oid = str(o.get("id") or o.get("orderId") or "")
        if oid not in bot_open and o.get("marketSlug") not in inflight:
            out.append(f"open order {oid} on {o.get('marketSlug')} is not one of the bot's")
    for oid in bot_open - exch_ids:
        out.append(f"order {oid} the bot thinks is open is not open on the exchange")
    return out


async def fetch_exchange(broker, expired: dict | None = None) -> tuple[dict, list]:
    d = await broker._req("GET", "/v1/portfolio/positions")
    o = await broker._req("GET", "/v1/orders/open")
    orders = o.get("orders") if isinstance(o, dict) else o
    if not isinstance(orders, list):
        orders = next((v for v in o.values() if isinstance(v, list)), []) if isinstance(o, dict) else []
    if expired is not None:
        expired.update(parse_expired(d))
    return parse_positions(d), orders


class Reconciler:
    def __init__(self, engine, broker, db, interval_s: float = 60, confirm_checks: int = 2,
                 venue: str = "polymarket_us", bot_orders_fn=None, closed_fn=None):
        """bot_orders_fn() -> {order_id: market slug} for every order the bot holds open right now
        (from the traders' brokers, so a kill/cancel is reflected immediately).
        closed_fn(slug) -> awaitable bool: the market no longer trades on Polymarket US. A position the
        bot still lists but the exchange doesn't, in a market that has closed or settled, is SETTLING (the
        trader books the payout shortly), not a mismatch. In an open market it still is one."""
        self.engine, self.broker, self.db = engine, broker, db
        self.bot_orders_fn = bot_orders_fn or (lambda: {})
        self.closed_fn = closed_fn
        self.interval, self.confirm = interval_s, confirm_checks
        self.venue = venue
        self.strikes = 0
        self.last: ReconResult | None = None
        self.halted_reason = ""

    async def check(self, orders: bool = True) -> ReconResult:
        expected, inflight = bot_expected(self.db, self.venue)
        bot_orders = self.bot_orders_fn() if orders else {}
        for v in bot_orders.values():                # resting profit locks don't make a market "in flight"
            slug, purpose = v if isinstance(v, tuple) else (v, "")
            if purpose != "lock":
                inflight.add(slug)
        expired: dict = {}
        exch, exch_open = await fetch_exchange(self.broker, expired)
        for slug, q in expired.items():              # the exchange itself says: settling
            want = expected.get(slug)
            if want is None or abs(q) <= TOL or abs(q - want) <= 0.005:
                inflight.add(slug)                   # past positions / quantities agree
            else:
                exch[slug] = q                       # settling, but not what the bot booked: a mismatch
        if self.closed_fn is not None:
            for slug, q in expected.items():         # bot holds, exchange shows none: closed market?
                if slug not in inflight and abs(exch.get(slug, 0.0)) <= TOL and abs(q) > TOL:
                    try:
                        if await self.closed_fn(slug):
                            inflight.add(slug)
                    except Exception as e:  # noqa: BLE001 - unknown status: judged as a normal mismatch
                        log.warning("reconcile: market status of %s unknown: %r", slug, e)
        mism = compare(expected, exch, inflight, set(bot_orders) if orders else None, exch_open)
        self.last = ReconResult(not mism, mism, exch, expected, sorted(inflight))
        return self.last

    async def _halt(self, mism: list[str], startup: bool) -> None:
        reason = "RECONCILE MISMATCH" + (" at startup" if startup else "") + ": " + "; ".join(mism)[:300]
        self.halted_reason = reason
        log.error(reason)
        try:
            store = getattr(self.engine, "trade_store", None)
            if store is not None:          # persists: stays halted across restarts until Resume
                store.set_state("kill_requested", {"ts": time.time(), "reason": reason})
            await self.engine.kill_all(reason)
        finally:
            self.engine.alert("risk", "Reconciliation mismatch - trading HALTED",
                              "; ".join(mism)[:500] + ". Check the Polymarket app, fix it, then Resume.", "warn")

    async def startup(self) -> ReconResult:
        """Run before trading starts: load positions from the exchange first and halt on any mismatch."""
        r = await self.check(orders=False)
        if not r.ok:
            await self._halt(r.mismatches, startup=True)
        return r

    async def tick(self) -> ReconResult | None:
        try:
            r = await self.check()
        except Exception as e:  # noqa: BLE001 - a failed check is not a mismatch, but it is reported
            log.warning("reconciliation check failed: %s", e)
            return None
        if r.ok:
            self.strikes = 0
            return r
        self.strikes += 1
        if self.strikes >= self.confirm:
            await self._halt(r.mismatches, startup=False)
            self.strikes = 0
        return r

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.tick()

    def snapshot(self) -> dict:
        r = self.last
        return {"ok": None if r is None else r.ok, "ts": None if r is None else r.ts,
                "mismatches": [] if r is None else r.mismatches, "strikes": self.strikes,
                "halted_reason": self.halted_reason}
