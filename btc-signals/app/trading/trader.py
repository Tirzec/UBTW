"""AutoTrader: turns signals into limit orders and manages exits, under strict risk limits.

Lifecycle of a position:
  opening  -> post-only BUY resting; filled (or partially filled) within order_timeout_s,
              otherwise canceled -> void
  open     -> exit rules checked every check_interval_s against the sellable bid
  closing  -> marketable SELL limit working; unfilled remainder is retried
  resolving-> held through the window close; settled from Polymarket's official outcome
  closed   -> P&L recorded (fees included), risk counters updated
"""
from __future__ import annotations

import asyncio
import math
import logging
import time

import json

from .broker import BrokerError, Maintenance, OrderState
from .risk import RiskManager, RiskState, local_day
from .rules import plan_entry, round_price, trigger_levels, value_position
from .store import TradeStore, bot_stats

log = logging.getLogger("trader")

ACTIVE = ("opening", "open", "closing", "resolving")


def entry_cost_guard(ec: dict, *, plan_price: float, fair: float | None, best_bid: float | None,
                     best_ask: float | None, fee_schedule: dict | None) -> str:
    """Skip entries that lose money the moment they fill (all limits optional, set per module).

    max_spread                 skip if best ask - best bid is wider than this
    min_price / max_price      skip very cheap / very expensive contracts (one tick is a huge %)
    min_edge_after_exit_cost   fair - entry price - (entry - bid) - exit fee per share must be >= this:
                               the edge has to survive selling straight back at the bid.
    Returns the reason to skip, or "" to go ahead."""
    from ..fees import fee_per_share
    if "max_spread" in ec and best_bid is not None and best_ask is not None \
            and best_ask - best_bid > float(ec["max_spread"]) + 1e-9:
        return f"spread {best_ask - best_bid:.2f} > {float(ec['max_spread']):.2f}"
    if "min_price" in ec and plan_price < float(ec["min_price"]) - 1e-9:
        return f"price {plan_price:.2f} < {float(ec['min_price']):.2f} (one tick is too large a share)"
    if "max_price" in ec and plan_price > float(ec["max_price"]) + 1e-9:
        return f"price {plan_price:.2f} > {float(ec['max_price']):.2f}"
    if "min_edge_after_exit_cost" in ec:
        if fair is None or best_bid is None:
            return "no bid to price the exit cost"
        exit_cost = max(0.0, plan_price - best_bid) + fee_per_share(best_bid, fee_schedule)
        net = fair - plan_price - exit_cost
        if net < float(ec["min_edge_after_exit_cost"]) - 1e-9:
            return (f"edge {fair - plan_price:+.3f} doesn't cover selling back at the bid "
                    f"({exit_cost:.3f}/share)")
    return ""


RULES_VERSION = "v2-2026-11-10"      # fast entries (book-driven, take ask at 8+ pts, 3 s re-quote) + hold winners


class AutoTrader:
    """One executor per strategy module. All modules share this order lifecycle, the audit
    log, the kill switch and (through GlobalRisk) the combined loss/exposure limits."""

    def __init__(self, engine, cfg: dict, broker, store: TradeStore, module=None,
                 exec_conf: dict | None = None, global_risk=None, min_paper_trades: int | None = None):
        self.engine = engine
        self.cfg = cfg
        self.tc = cfg.get("trading", {}) or {}
        if module is None:
            from ..modules.crypto_updown import CryptoUpDownModule
            module = CryptoUpDownModule(engine, cfg)
        self.module = module
        xc = exec_conf if exec_conf is not None else self.tc
        self.ec = xc.get("entry", {}) or {}
        self.xc = xc.get("exits", {}) or {}
        self.broker = broker
        self.mode = broker.mode
        self.store = store
        self.global_risk = global_risk
        self.min_paper = int(min_paper_trades if min_paper_trades is not None
                             else self.tc.get("min_paper_trades_for_live", 300))
        from .store import paper_count_from
        self.paper_since = paper_count_from(xc)
        self.risk_key = "risk" if module.name == "crypto_updown" else f"risk:{module.name}"
        if self.mode == "live":
            self.risk_key = "live:" + self.risk_key       # paper streaks/pauses never carry into live
        saved = store.get_state(self.risk_key, {}) or {}
        st = RiskState(**{k: v for k, v in saved.items() if k in RiskState.__dataclass_fields__})
        self.risk = RiskManager(xc.get("risk", {}) or {}, st)
        self._last_resume_seen = float(store.get_state("resume_requested") or 0)
        if global_risk is not None:
            global_risk.register(self)
        self._recompute_day()
        self.orders: dict[str, OrderState] = {}
        self.last_close: dict[str, float] = {}
        self.status_note = ""
        self.last_balance: float | None = None
        self._limit_alerted: set = set()
        self._lock_rejected: dict[int, float] = {}      # position id -> time a post-only lock was rejected
        self._cancel_sent: dict[str, float] = {}        # order id -> when we last asked the exchange to cancel
        self._tick_last: dict[int, tuple] = {}          # position id -> (ts, bid, decision) last trace row
        self._latency: list = []                        # recent exit latencies (ms)
        self._entry_latency: list = []                  # recent book update -> entry order sent (ms)
        self._entry_latency_alerted = 0.0
        self._sig_last: dict = {}                       # signal log throttle
        self._book_event_obj: asyncio.Event | None = None    # created inside the running loop (Python 3.9)
        self._lock_obj: asyncio.Lock | None = None      # entry pass and position management never interleave
        self._entry_task: asyncio.Task | None = None
        self._latency_alerted = 0.0
        self._last_balance_check = -1e18
        self._last_hb = 0.0
        self._task: asyncio.Task | None = None
        self.started = time.time()

    # ------------------------------------------------------------ persistence
    def _recompute_day(self) -> None:
        """Today's P&L comes from the trade log, not memory, so restarts can't reset the limit."""
        now = time.time()
        self.risk.roll_day(now)
        day_start = time.mktime(time.strptime(local_day(now), "%Y-%m-%d"))
        rows = self.store.closed_positions(since=day_start)
        self.risk.state.day_pnl = sum(r["pnl"] or 0 for r in rows)
        self.risk.state.day_trades = len(rows)
        st = self.risk.state
        if st.day_stopped_until and st.day_stop_reason != "streak" and (
                self.risk.daily_loss_limit is None or st.day_pnl > -self.risk.daily_loss_limit):
            # the daily limit was raised since the stop: today's loss is now inside it, so lift the stop
            st.day_stopped_until = 0.0
            self.store.event("risk", f"daily stop lifted: today {st.day_pnl:+.2f} is within the new limit "
                                     f"-{self.risk.daily_loss_limit:g}", "WARNING")

    def _save(self) -> None:
        self.store.set_state(self.risk_key, {k: v for k, v in self.risk.state.__dict__.items()
                                             if k != "api_errors"})

    def _alert(self, title: str, body: str = "", level: str = "info", kind: str = "trade") -> None:
        tag = self.mode.upper() if self.module.name == "crypto_updown" else \
            f"{self.mode.upper()} {self.module.name}"
        self.engine.alert(kind, f"[{tag}] {title}", body, level)

    # ------------------------------------------------------------ control
    async def kill(self, reason: str = "STOP pressed") -> int:
        """Kill switch: cancel every open order and halt all trading (entries AND exits)."""
        self.risk.halt(f"KILL SWITCH: {reason}")    # first: no new entry gets past the blockers from now on
        self._save()
        # wait for an entry/exit already in flight (e.g. inside place_limit) to record its order id, so
        # cancel_all and the position walk below see it. Never wait forever: the kill switch must finish.
        locked = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=10)
            locked = True
        except asyncio.TimeoutError:
            self.store.event("warning", "kill: trading step still busy after 10 s, cancelling anyway", "WARNING")
        try:
            return await self._kill_body(reason)
        finally:
            if locked:
                self._lock.release()

    async def _kill_body(self, reason: str) -> int:
        n = 0
        try:
            n = await self.broker.cancel_all()
        except Exception as e:  # noqa: BLE001
            self.store.event("error", f"cancel_all failed during kill: {e}", "ERROR")
        for p in self.store.active_positions():
            try:
                if p["status"] == "opening":
                    await self._finish_entry(p)    # reads the entry's final fills (live) before deciding
                elif p["status"] == "closing":
                    if p.get("exit_order"):
                        await self._final_read(p["exit_order"])
                    await self._poll_exit(self.store.position(p["id"]), time.time() + 3600)
                    p2 = self.store.position(p["id"])
                    if p2["status"] == "closing":
                        self.store.update_position(p["id"], status="open", exit_order=None)
                elif p.get("lock_order"):
                    await self._lock_poll(p)
            except Exception as e:  # noqa: BLE001 - the kill switch must finish
                self.store.event("error", f"kill: position {p['id']}: {e}", "ERROR")
        self.store.event("kill", reason, "WARNING", canceled=n)
        self._alert("KILL SWITCH - trading halted", f"{reason}. Canceled {n} open orders. "
                    "Open positions are left for you to manage.", "warn", "risk")
        return n

    def resume(self) -> None:
        self.risk.resume()
        self.store.set_state("kill_requested", None)
        self._save()
        self.store.event("resume", "trading resumed by user", "WARNING")
        self._alert("Trading resumed", "", "info", "risk")

    def _hard_pause(self, reason: str) -> None:
        self._save()
        self.store.event("pause", reason, "WARNING")
        self._alert("Auto-trader PAUSED", reason + ". Press Resume after checking.", "warn", "risk")

    def _api_error(self, where: str, e: Exception) -> None:
        self.store.event("api_error", f"{where}: {e}", "ERROR")
        if self.mode == "live":
            self._alert(f"Error: {where}", str(e)[:300], "warn", "error")
        reason = self.risk.record_api_error(time.time())
        if reason:
            self._hard_pause(reason)

    # ------------------------------------------------------------ main loop
    async def recover(self) -> None:
        """After a restart: cancel stale orders and reconcile positions left mid-flight."""
        try:
            n = await self.broker.cancel_all()
            if n:
                self.store.event("recover", f"canceled {n} stale open orders at startup", "WARNING")
        except Exception as e:  # noqa: BLE001
            self._api_error("startup cancel_all", e)
        for p in self.store.active_positions():
            for col, purpose in (("entry_order", "entry"), ("exit_order", "exit"), ("lock_order", "lock")):
                oid = p.get(col)
                if not oid or oid in self.broker.orders:
                    continue
                row = self.store.db.query("SELECT * FROM bot_orders WHERE order_id=? AND mode=?",
                                          (oid, self.mode))
                if row:
                    await self.broker.adopt(row[0])
            if p["status"] == "opening":
                await self._finish_entry(self.store.position(p["id"]))
            elif p["status"] == "closing":
                await self._poll_exit(self.store.position(p["id"]), time.time() + 3600)
            p = self.store.position(p["id"])
            if p["status"] == "open" and p.get("lock_order") in self.broker.orders:
                await self._lock_poll(p)          # book fills the lock got while the bot was down

    async def start(self) -> None:
        await self.recover()
        self.store.event("start", f"auto-trader started in {self.mode.upper()} mode", "WARNING",
                         risk=self.risk.summary())
        self._task = asyncio.create_task(self._loop(), name="autotrader")
        bs = self._book_store()
        if self._event_driven() and bs is not None:
            bs.listeners.append(self._book_event.set)
            self._entry_task = asyncio.create_task(self._entry_loop(), name="autotrader-entries")

    async def stop(self) -> None:
        try:                                        # never cancel in the middle of placing an order
            await asyncio.wait_for(self._lock.acquire(), timeout=10)
            got = True
        except asyncio.TimeoutError:
            got = False
        try:
            for t in (self._task, self._entry_task):
                if t:
                    t.cancel()
                    await asyncio.gather(t, return_exceptions=True)
        finally:
            if got:
                self._lock.release()
        bs = self._book_store()
        if bs is not None and self._book_event.set in bs.listeners:
            bs.listeners.remove(self._book_event.set)
        await self.broker.close()

    @property
    def _book_event(self) -> asyncio.Event:
        if self._book_event_obj is None:
            self._book_event_obj = asyncio.Event()
        return self._book_event_obj

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    def _book_store(self):
        clob = getattr(self.engine, "clob", None)
        st = getattr(clob, "store", None)
        return st if hasattr(st, "listeners") else None

    def _event_driven(self) -> bool:
        return self.module.name == "crypto_updown" and self.ec.get("event_driven", True) not in (False, "off")

    async def _entry_loop(self) -> None:
        """Entries react to every order-book update (websocket or poll), not to a timer. Debounced: at most
        one entry check per min_signal_gap_s; at least one per second without updates."""
        gap = float(self.ec.get("min_signal_gap_s", 0.05))
        while True:
            try:
                await asyncio.wait_for(self._book_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._book_event.clear()
            try:
                if not self.store.get_state("kill_requested"):     # STOP wins even between timer ticks
                    async with self._lock:
                        await self._entry_pass(time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("entry check failed")
                self._api_error("entry check", e)
            await asyncio.sleep(gap)

    async def _loop(self) -> None:
        interval = float(self.xc.get("check_interval_s", 1))
        fast = float(self.xc.get("fast_check_interval_s", interval))
        while True:
            t0 = time.time()
            active = self.store.active_positions()
            if hasattr(self.engine, "clob") and hasattr(self.engine.clob, "set_fast") and \
                    self.module.name == "crypto_updown":
                self.engine.clob.set_fast({p["slug"] for p in active})   # their books poll ~4x a second
            try:
                await self.tick(t0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("auto-trader tick failed")
                self._api_error("tick", e)
            wait = fast if active else interval
            await asyncio.sleep(max(0.05, wait - (time.time() - t0)))

    async def tick(self, now: float) -> None:
        # CLI kill switch / resume requests arrive through the DB
        req = self.store.get_state("kill_requested")
        if req and not (self.risk.state.halted and self.risk.state.halt_reason.startswith("KILL")):
            await self.kill("STOP from command line")
        rr = float(self.store.get_state("resume_requested") or 0)
        if rr > self._last_resume_seen:          # every module sees a resume request once
            self._last_resume_seen = rr
            self.resume()
        await self._housekeeping(now)
        async with self._lock:
            if self.risk.exits_allowed():
                for p in self.store.active_positions():
                    try:
                        await self._manage(p, now)
                    except Maintenance as m:
                        self.risk.state.maintenance_until = now + m.retry_after
                    except BrokerError as e:
                        self._api_error(f"position {p['id']}", e)
            if self._entry_task is None or self._entry_task.done():
                await self._entry_pass(now)        # timer-driven fallback (no book feed / other modules)
        self._save()

    async def _entry_pass(self, now: float) -> None:
        sigs = self.module.signals(now)
        if not sigs:
            self.status_note = ""
        for sig in sigs:
            try:
                await self._maybe_enter(sig, now)
            except Maintenance as m:
                self.risk.state.maintenance_until = now + m.retry_after
                self.store.event("maintenance", str(m), "WARNING")
            except BrokerError as e:
                self._api_error(f"entry {sig.label}", e)

    async def _housekeeping(self, now: float) -> None:
        if self.mode == "live" and now - self._last_hb >= 5:
            self._last_hb = now
            try:
                await self.broker.heartbeat()
            except Exception as e:  # noqa: BLE001
                self._api_error("heartbeat", e)
        if now - self._last_balance_check >= float(self.tc.get("balance_check_s", 30)):
            self._last_balance_check = now
            try:
                self.last_balance = await self.broker.balance()
                reason = self.risk.check_balance(self.last_balance)
                if reason:
                    self._hard_pause(reason)
            except Exception as e:  # noqa: BLE001
                self._api_error("balance", e)

    # ------------------------------------------------------------ entries
    def stake_now(self, sig=None) -> float | None:
        """Dollars for the next entry: trading.entry.stake_pct of current buying power if set (e.g. 10%),
        else the fixed stake_usd; never above risk.max_stake_usd unless that is 'off'."""
        pct = self.ec.get("stake_pct")
        if pct not in (None, False, "off"):
            if self.last_balance is None:
                return None
            stake = float(pct) * float(self.last_balance)
        else:
            stake = float((sig.stake_usd if sig is not None and sig.stake_usd else None) or self.ec.get("stake_usd", 5))
        if self.risk.max_stake is not None:
            stake = min(stake, self.risk.max_stake)
        return round(stake, 2)

    def _exposure(self) -> float:
        """USDC at risk: unfilled entry orders + cost basis of shares still held."""
        tot = 0.0
        for p in self.store.active_positions():
            if p["status"] == "opening":
                o = self.broker.orders.get(p["entry_order"] or "")
                tot += o.price * o.size if o else 0.0
            elif p["shares"]:
                held = p["shares"] - (p["exit_shares"] or 0)
                tot += ((p["cost"] or 0) + (p["entry_fee"] or 0)) * held / p["shares"]
        return tot

    def _window_busy(self, slug: str, now: float) -> bool:
        if any(p["slug"] == slug for p in self.store.active_positions()):
            return True
        n = self.store.db.query("SELECT COUNT(*) n FROM bot_positions WHERE mode=? AND slug=? AND "
                                "status!='void'", (self.mode, slug))[0]["n"]
        if n >= int(self.ec.get("max_entries_per_window", 2)):
            return True
        return now - self.last_close.get(slug, 0) < float(self.ec.get("reentry_cooldown_s", 20))

    def _log_signal(self, sig, decision: str, reason: str = "", position_id=None) -> None:
        """Signal log (bot_signals), throttled: a row when the decision/reason changes, else every 10 s."""
        if (sig.extra or {}).get("strategy") != "fair_value":
            return
        key = (sig.market_id, sig.side)
        last = self._sig_last.get(key)
        now = time.time()
        if decision != "entered" and last and last[1] == (decision, reason[:40]) and now - last[0] < 10:
            return
        self._sig_last[key] = (now, (decision, reason[:40]))
        try:
            self.store.signal(slug=sig.market_id, side=sig.side, edge=round(sig.edge, 4),
                              ask=(sig.extra or {}).get("ask"), fair=round(sig.fair_prob, 4), decision=decision,
                              reason=reason[:200], rules=RULES_VERSION, position_id=position_id)
        except Exception:  # noqa: BLE001 - logging must never block trading
            log.exception("signal log failed")

    def _skip(self, sig, reason: str) -> None:
        self.status_note = f"{sig.label} {sig.side} signal skipped: {reason}"
        self._log_signal(sig, "skipped", reason)

    async def _maybe_enter(self, sig, now: float) -> None:
        m = self.module.market(sig.market_id)
        if m is None or (m.entry_deadline is not None and now >= m.entry_deadline):
            return
        stake = self.stake_now(sig)
        if stake is None:
            self._skip(sig, "balance not known yet (stake is a % of it)")
            return
        if self.risk.max_exposure_pct is not None and self.last_balance is not None:
            self.risk.max_exposure = self.risk.max_exposure_pct * self.last_balance
        active = self.store.active_positions()
        blockers = self.risk.entry_blockers(now=now, stake=stake, open_exposure=self._exposure(),
                                            window_busy=self._window_busy(sig.market_id, now),
                                            degraded=self.module.degraded())
        blockers += self.module.entry_blockers(sig, active, now)
        if self.mode == "live" and self.last_balance is not None and self.last_balance < stake:
            blockers.append(f"buying power ${self.last_balance:.2f} < stake ${stake:.2f}")   # skip, no halt
        if self.global_risk is not None:
            gb = self.global_risk.entry_blockers(now, stake)
            for b in gb:                         # combined loss limit / phase trade cap: tell the phone once a day
                if b.startswith(("combined daily loss", "live phase 1")):
                    key = f"{local_day(now)}:{b.split(':')[0][:40]}"
                    if key not in self._limit_alerted:
                        self._limit_alerted.add(key)
                        self._alert("Daily limit reached - no more entries today", b, "warn", "risk")
            blockers += gb
        if blockers:
            self._skip(sig, blockers[0])
            return
        side, token = sig.side, sig.token
        book = self.module.book(token) or {"bids": [], "asks": []}
        limit = (sig.extra or {}).get("limit")
        if limit is not None:
            # strategy-chosen limit price (at or slightly below the ask); rests up to order_timeout_s
            from .rules import EntryPlan, round_down
            from ..fees import fee_usdc
            shares = round(round_down(stake / limit, m.size_step), 2)
            plan = (EntryPlan(True, "", limit, shares, round(shares * limit, 6), False,
                              fee_usdc(shares, limit, m.fee_schedule)) if shares >= m.min_size
                    else EntryPlan(False, f"${stake:g} buys {shares:.2f} < minimum {m.min_size:g}"))
        else:
            plan = plan_entry(best_bid=book["bids"][0][0] if book["bids"] else None,
                              best_ask=book["asks"][0][0] if book["asks"] else None, stake_usd=stake,
                              tick=m.tick_size, min_size=m.min_size, size_step=m.size_step,
                              style=self.ec.get("order_style", "maker"), fee_schedule=m.fee_schedule)
        if not plan.ok:
            self._skip(sig, plan.reason)
            return
        why = entry_cost_guard(self.ec, plan_price=plan.price, fair=sig.fair_prob,
                               best_bid=book["bids"][0][0] if book["bids"] else None,
                               best_ask=book["asks"][0][0] if book["asks"] else None,
                               fee_schedule=m.fee_schedule)
        if why:
            self._skip(sig, why)
            return
        if self.risk.state.halted or self.store.get_state("kill_requested"):
            self._skip(sig, "STOP requested")       # kill() runs on the next tick: never enter meanwhile
            return
        pid = self.store.new_position(slug=sig.market_id, duration=sig.label, side=side, token=token,
                                      condition_id=m.condition_id, status="opening",
                                      entry_price=plan.price, p_model=sig.fair_prob, edge=sig.edge,
                                      opened_ts=now, league=sig.league, group_key=sig.group or sig.market_id,
                                      bet_type=sig.bet_type, confidence=sig.confidence,
                                      starts_at=sig.starts_at,
                                      strategy=(sig.extra or {}).get("strategy"),
                                      time_left_s=(sig.extra or {}).get("time_left"),
                                      rules=RULES_VERSION if (sig.extra or {}).get("strategy") else None,
                                      signal_ask=(sig.extra or {}).get("ask"),
                                      signal_book_ts=(sig.extra or {}).get("book_ts"),
                                      reasons=json.dumps([r.as_dict() for r in sig.reasons][:5]))
        t_send = time.time()
        o = await self.broker.place_limit(token=token, side="BUY", price=plan.price, size=plan.shares,
                                          post_only=plan.post_only,
                                          ioc=False if limit is not None else None)
        self.store.order(o, pid, "entry")
        self.store.update_position(pid, entry_order=o.order_id, order_sent_ts=t_send)
        self._log_signal(sig, "entered", f"BUY @ {plan.price:.2f} (ask {(sig.extra or {}).get('ask')})", pid)
        self._record_entry_latency(pid, sig, now, t_send)
        self.store.event("order", f"BUY {side} {plan.shares:.2f} @ {plan.price:.2f} "
                         f"({'post-only' if plan.post_only else 'limit'}) {sig.market_id}", position=pid,
                         order=o.order_id, status=o.status, edge=round(sig.edge, 4),
                         p=round(sig.fair_prob, 4))
        if o.status == "rejected":
            self.store.update_position(pid, status="void", exit_reason=f"entry rejected: {o.error}",
                                       closed_ts=now)
            if o.error.startswith("post_only_would_cross"):
                return                               # book moved; try again next tick
            raise BrokerError(f"entry rejected: {o.error}")
        if o.filled > 0:
            self.store.update_position(pid, first_fill_ts=time.time())
        if o.status == "filled":
            await self._finish_entry(self.store.position(pid))
        # otherwise it rests; _manage() polls it and cancels after order_timeout_s

    async def _finish_entry(self, p: dict) -> None:
        """Convert a (partially) filled entry order into an open position, or void it."""
        o = self.broker.orders.get(p["entry_order"] or "")
        if o is None:
            self.store.update_position(p["id"], status="void", exit_reason="entry order lost on restart",
                                       closed_ts=time.time())
            return
        if not o.done:
            o = await self.broker.cancel(o.order_id)
            self.store.order(o, p["id"], "entry")
            self.store.event("cancel", f"entry order {o.order_id} canceled "
                             f"(filled {o.filled:.2f}/{o.size:.2f})", position=p["id"])
        if self.mode == "live" and not getattr(o, "_final", False):
            # a cancel / cancel-all marks the order canceled locally; fills that raced it are only visible
            # on a fresh read. Never book or void an entry on unconfirmed numbers.
            fo = await self._final_read(o.order_id, tries=2, wait_s=0.0)
            if fo is None:
                self.store.event("warning", f"entry {o.order_id}: final state not confirmed yet, retrying",
                                 "WARNING", position=p["id"])
                return                             # stays 'opening'; _manage retries next tick
            fo._final = True
            o = fo
            self.store.order(o, p["id"], "entry")
        if o.filled <= 0:
            self.store.update_position(p["id"], status="void", exit_reason="entry not filled in time",
                                       closed_ts=time.time())
            return
        m = self.module.market(p["slug"])
        tp, sl = trigger_levels(shares=o.filled, cost_basis=o.notional + o.fee,
                                take_profit_pct=float(self.xc.get("take_profit_pct", 0.08)),
                                stop_loss_pct=float(self.xc.get("stop_loss_pct", 0.10)),
                                fee_schedule=m.fee_schedule if m else None)
        self.store.update_position(p["id"], status="open", shares=o.filled, cost=o.notional,
                                   entry_fee=o.fee, entry_price=round(o.avg_price, 6), filled_ts=time.time(),
                                   tp_price=tp, sl_price=sl)
        self.store.event("fill", f"BOUGHT {p['side']} {o.filled:.2f} @ {o.avg_price:.3f} fee {o.fee:.4f}",
                         position=p["id"], order=o.order_id)
        self._alert(f"Bought {p['duration']} {p['side']} @ {o.avg_price:.2f}",
                    f"{o.filled:.1f} shares, ${o.notional:.2f}. TP bid ≥{tp:.2f}, SL bid ≤{sl:.2f}.")

    def _record_entry_latency(self, pid: int, sig, t_signal: float, t_send: float) -> None:
        """Book update received -> entry order sent. Alert if the median is regularly over the limit."""
        book_ts = (sig.extra or {}).get("book_ts")
        if not book_ts:
            return
        ms = (t_send - book_ts) * 1000
        self.store.event("entry_latency", f"book update -> order {ms:.0f} ms (signal -> order "
                         f"{(t_send - t_signal) * 1000:.0f} ms)", position=pid, book_to_order_ms=round(ms))
        self._entry_latency = (self._entry_latency + [ms])[-20:]
        limit = float(self.ec.get("entry_latency_alert_ms", 300))
        if len(self._entry_latency) >= 5:
            med = sorted(self._entry_latency)[len(self._entry_latency) // 2]
            if med > limit and time.time() - self._entry_latency_alerted > 3600:
                self._entry_latency_alerted = time.time()
                self.store.event("warning", f"slow entries: median book->order {med:.0f} ms > {limit:.0f} ms",
                                 "WARNING")
                self._alert("Slow entries", f"median book update -> buy order {med:.0f} ms over the last "
                            f"{len(self._entry_latency)} entries (limit {limit:.0f} ms)", "warn", "error")

    # ------------------------------------------------------------ exits
    async def _manage(self, p: dict, now: float) -> None:
        m = self.module.market(p["slug"])
        if p["status"] == "opening":
            o = await self.broker.poll(p["entry_order"])
            self.store.order(o, p["id"], "entry")
            if o.filled > 0 and not p.get("first_fill_ts"):
                self.store.update_position(p["id"], first_fill_ts=now)
            timeout = float(self.ec.get("order_timeout_s", 8))
            if o.filled >= o.size - 1e-9 or now - o.created >= timeout or \
                    (m and m.entry_deadline is not None and now >= m.entry_deadline):
                await self._finish_entry(p)
            return
        if p["status"] == "closing":
            await self._poll_exit(p, now)
            return
        if p["status"] == "resolving" or (m and m.hold_phase_from is not None and now >= m.hold_phase_from):
            p = await self._cancel_lock(p)
            if p is None:                          # lock not confirmed canceled yet: try again next tick
                return
            if p["status"] == "closed":
                return
            await self._resolve(p, now)
            return
        # 1) book any fills of the resting lock, 2) exit rules, 3) only while holding: keep a lock working
        p = await self._lock_poll(p)
        if p is None or p["status"] != "open":
            return
        if await self._evaluate_exit(p, m, now):
            return
        p = self.store.position(p["id"])
        if p["status"] == "open":
            p = await self._lock_reprice(p, m, now)
            if p is None or p["status"] != "open":
                return                             # old lock not confirmed canceled yet: next tick
            await self._lock_place(p, m, now)

    # ------------------------------------------------------------ resting profit-lock SELL
    def _lock_account(self, p: dict, o) -> dict:
        """Book NEW fills of the lock order into the position (maker: no fee). What has already been booked
        for the current lock is stored on the position (lock_filled/lock_notional/lock_fee), so a restart
        can neither lose nor double-count fills. Returns fresh p."""
        prev = (p.get("lock_filled") or 0.0, p.get("lock_notional") or 0.0, p.get("lock_fee") or 0.0)
        d_sh, d_not, d_fee = o.filled - prev[0], o.notional - prev[1], o.fee - prev[2]
        if d_sh <= 1e-9:
            return p
        sold = (p["exit_shares"] or 0) + d_sh
        proceeds = (p["exit_proceeds"] or 0) + d_not
        fee = (p["exit_fee"] or 0) + d_fee
        self.store.update_position(p["id"], exit_shares=sold, exit_proceeds=proceeds, exit_fee=fee,
                                   exit_price=proceeds / sold if sold else None,
                                   lock_filled=o.filled, lock_notional=o.notional, lock_fee=o.fee)
        self.store.event("fill", f"SOLD {p['side']} {d_sh:.2f} @ {d_not / d_sh:.3f} (resting profit lock)",
                         position=p["id"], order=o.order_id)
        self._alert(f"Profit lock filled: {p['duration']} {p['side']} {d_sh:g} @ {d_not / d_sh:.2f}",
                    "resting sell order filled with no reaction delay")
        p = self.store.position(p["id"])
        if sold >= p["shares"] - 1e-6:
            self.store.update_position(p["id"], lock_order=None, exit_reason="profit lock filled",
                                       exit_trigger="profit_lock")
            self._close(self.store.position(p["id"]), payout=0.0, now=time.time(),
                        reason=f"resting profit lock filled at {d_not / d_sh:.2f}", trigger="profit_lock")
            return self.store.position(p["id"])
        return p

    async def _final_read(self, oid: str, tries: int = 10, wait_s: float = 0.2):
        """LIVE: re-read an order from the exchange until it reports a final state (filled / canceled /
        rejected). Needed after a cancel or cancel-all, which mark orders canceled locally without reading
        the fills that raced them. Returns the order, or None if it is still not final."""
        o = self.broker.orders.get(oid)
        if o is None:
            return None
        if self.mode != "live":
            return o if o.done else await self.broker.poll(oid)
        for i in range(tries):
            o.status = "live"                      # force a fresh read
            o = await self.broker.poll(oid)
            if o.done:
                return o
            if i < tries - 1:
                await asyncio.sleep(wait_s)
        return None

    async def _lock_refresh(self, oid: str):
        o = self.broker.orders.get(oid)
        if o is None:
            return None
        if o.done and self.mode == "live" and not getattr(o, "_final", False):
            o2 = await self._final_read(oid)       # canceled by a cancel-all: read its final fills once
            if o2 is not None:
                o2._final = True
                return o2
            return o
        return await self.broker.poll(oid)

    def _lock_price(self, p: dict, m) -> float | None:
        """Resting profit-lock price: max(entry + profit_lock_offset, fair value + profit_lock_fair_margin),
        capped at profit_lock_price, on the tick grid (rounded up). Fair value unknown -> entry + offset."""
        cap = self.xc.get("profit_lock_price")
        if not cap:
            return None
        cap = float(cap)
        off = self.xc.get("profit_lock_offset")
        entry = p.get("entry_price")
        if off is None or entry is None:
            return cap
        tick = m.tick_size if m else 0.01
        target = float(entry) + float(off)
        margin = self.xc.get("profit_lock_fair_margin")
        if margin not in (None, False, "off") and hasattr(self.module, "fair"):
            fair, _ = self.module.fair(p)
            if fair is not None:
                target = max(target, float(fair) + float(margin))
        price = round(math.ceil(target / tick - 1e-6) * tick, 6)
        return min(price, cap)

    async def _lock_reprice(self, p: dict, m, now: float):
        """Fair value moved: move the resting lock to the new target (>= reprice_ticks away, at most every
        reprice_s). Uses the safe cancel; returns fresh p, or None if the cancel is not confirmed yet."""
        oid = p.get("lock_order")
        if not oid or m is None:
            return p
        o = self.broker.orders.get(oid)
        if o is None or o.done or o.purpose not in ("lock", "", None):
            return p
        if m.hold_phase_from is not None and now >= m.hold_phase_from - 1:
            return p
        target = self._lock_price(p, m)
        if not target:
            return p
        tick = m.tick_size or 0.01
        if abs(target - o.price) < int(self.xc.get("profit_lock_reprice_ticks", 2)) * tick - 1e-9:
            return p
        if now - (o.created or 0) < float(self.xc.get("profit_lock_reprice_s", 3)):
            return p
        self.store.event("order", f"moving profit lock {o.price:.2f} -> {target:.2f} (fair value moved)",
                         position=p["id"], order=oid)
        return await self._cancel_lock(p)

    async def _lock_poll(self, p: dict):
        """Refresh the current lock order (if any), book its fills, forget it once it is finished."""
        oid = p.get("lock_order")
        if not oid:
            return p
        o = await self._lock_refresh(oid)
        if o is None:
            # unknown order (e.g. adopt failed): keep the id so reconciliation can flag a real problem
            return p
        self.store.order(o, p["id"], "lock")
        p = self._lock_account(p, o)
        if p["status"] != "open":
            return p
        if o.done:
            self.store.update_position(p["id"], lock_order=None)
            p = self.store.position(p["id"])
        return p

    async def _lock_place(self, p: dict, m, now: float) -> None:
        """Keep a resting SELL for the held shares at the lock price (only called while holding)."""
        price = self._lock_price(p, m) if m is not None else None
        if not price or m is None or p.get("lock_order"):
            return
        held = round(p["shares"] - (p["exit_shares"] or 0), 2)
        if held < max(m.min_size, 0.01) or (m.hold_phase_from is not None and now >= m.hold_phase_from - 1):
            return
        book = self.module.book(p["token"]) or {}
        if book.get("bids") and book["bids"][0][0] >= price - 1e-9:
            return                                 # bid already there: the exit check sells at the bid
        if now - self._lock_rejected.get(p["id"], 0) < 2.0:
            return                                 # don't hammer the exchange with rejected post-only orders
        o = await self.broker.place_limit(token=p["token"], side="SELL", price=float(price), size=held,
                                          post_only=True)
        o.purpose = "lock"
        self.store.order(o, p["id"], "lock")
        if o.status == "rejected":
            self._lock_rejected[p["id"]] = now
            return
        self.store.update_position(p["id"], lock_order=o.order_id, lock_filled=0.0, lock_notional=0.0,
                                   lock_fee=0.0)
        self.store.event("order", f"resting profit lock: SELL {p['side']} {held:.2f} @ {float(price):.2f}",
                         position=p["id"], order=o.order_id)
        if o.filled:
            self._lock_account(self.store.position(p["id"]), o)

    async def _cancel_lock(self, p: dict):
        """Cancel the resting lock and book every fill it got. Returns fresh p, or None if the exchange has
        not confirmed the cancel yet (then the caller must NOT sell: those shares may still be sold)."""
        oid = p.get("lock_order")
        if not oid:
            return p
        o = self.broker.orders.get(oid)
        if o is not None:
            sent = self._cancel_sent.get(oid, 0.0)
            if not o.done and time.time() - sent > 5.0:
                self._cancel_sent[oid] = time.time()
                o = await self.broker.cancel(oid)
            if self.mode == "live":
                o = await self._final_read(oid, tries=2, wait_s=0.0)   # no sleeping inside the 250 ms tick
                if o is None:
                    self.store.event("warning", f"lock {oid}: cancel not confirmed yet, sale postponed",
                                     "WARNING", position=p["id"])
                    return None
            self.store.order(o, p["id"], "lock")
            p = self._lock_account(p, o)
        if p["status"] != "closed":
            self.store.update_position(p["id"], lock_order=None)
        return self.store.position(p["id"])

    def _fair(self, p: dict) -> tuple[float | None, bool]:
        return self.module.fair(p)

    async def _evaluate_exit(self, p: dict, m, now: float) -> None:
        book = self.module.book(p["token"]) or {"bids": [], "asks": []}
        shares = p["shares"] - (p["exit_shares"] or 0)
        basis = (p["cost"] + p["entry_fee"]) * (shares / p["shares"]) if p["shares"] else 0
        val = value_position(shares=shares, cost_basis=basis, bids=book["bids"],
                             fee_schedule=m.fee_schedule if m else None)
        t_decide = time.time()
        dec = self.module.exit_decision(p, val, now)
        lock_px = self._lock_price(p, m)
        if (dec.action != "SELL" and lock_px and book["bids"] and book["bids"][0][0] >= lock_px - 1e-9
                and val.shares_sellable > 0):
            # buyers already at/above the profit-lock price (no lock resting there yet): sell at the bid
            from .rules import ExitDecision
            dec = ExitDecision("SELL", f"bid {book['bids'][0][0]:.2f} >= profit-lock price {lock_px:.2f}",
                               "profit_lock")
        self.store.update_position(p["id"], last_value=val.net, last_pnl=val.pnl,
                                   last_note=dec.reason[:200])
        self._trace(p, book, dec, m, now)
        if dec.action != "SELL" or not book["bids"]:
            return False
        if p.get("lock_order"):                    # sell rule fired first: pull the resting lock, then sell
            p = await self._cancel_lock(p)
            if p is None:                          # cancel not confirmed: never risk selling twice
                return True
            if p["status"] != "open":
                return True
            shares = p["shares"] - (p["exit_shares"] or 0)
            if shares <= 1e-6:
                return True
            book = self.module.book(p["token"]) or {"bids": [], "asks": []}
            if not book["bids"]:
                return True                        # never sell into an empty book; next tick re-checks
        tick = m.tick_size if m else 0.01
        floor = round_price(max(tick, book["bids"][0][0] - int(self.xc.get("exit_slippage_ticks", 2)) * tick), tick)
        t_send = time.time()
        o = await self.broker.place_limit(token=p["token"], side="SELL", price=floor, size=round(shares, 2),
                                          post_only=False)
        o.purpose = "exit"
        self._record_latency(p, book, t_decide, t_send, time.time(), dec)
        self.store.order(o, p["id"], "exit")
        fair_now, _ = self.module.fair(p)
        self.store.update_position(p["id"], status="closing", exit_order=o.order_id,
                                   exit_reason=dec.reason, exit_trigger=dec.trigger, fair_at_exit=fair_now)
        self.store.event("order", f"SELL {p['side']} {shares:.2f} @ >= {floor:.2f}: {dec.reason}",
                         position=p["id"], order=o.order_id, status=o.status)
        if o.status == "rejected":
            self.store.update_position(p["id"], status="open", exit_order=None)
            raise BrokerError(f"exit rejected: {o.error}")
        await self._poll_exit(self.store.position(p["id"]), now)
        return True

    def _trace(self, p: dict, book: dict, dec, m, now: float) -> None:
        """Per-position trace: a row when the bid or decision changes, at least every second."""
        bid = book["bids"][0][0] if book["bids"] else None
        last = self._tick_last.get(p["id"])
        if last and last[1] == bid and last[2] == dec.action and now - last[0] < 1.0:
            return
        self._tick_last[p["id"]] = (now, bid, dec.action)
        fair, _ = self.module.fair(p)
        self.store.tick(p["id"], bid=bid, bid_qty=book["bids"][0][1] if book["bids"] else None,
                        ask=book["asks"][0][0] if book.get("asks") else None, fair=fair, decision=dec.action,
                        trigger=dec.trigger, book_ts=book.get("updated"),
                        seconds_left=(m.hold_phase_from - now) if (m and m.hold_phase_from) else None)

    def _record_latency(self, p: dict, book: dict, t_decide: float, t_send: float, t_ack: float, dec) -> None:
        """Exchange time of the book that triggered the sell -> order sent -> acknowledged."""
        book_ts = book.get("updated") or t_decide
        data_ms = (t_decide - book_ts) * 1000
        send_ms = (t_send - book_ts) * 1000
        ack_ms = (t_ack - t_send) * 1000
        self.store.event("exit_latency", f"price->order {send_ms:.0f} ms (data age {data_ms:.0f} ms, "
                         f"decide->send {(t_send - t_decide) * 1000:.0f} ms, ack {ack_ms:.0f} ms) {dec.trigger}",
                         position=p["id"], price_to_order_ms=round(send_ms), data_age_ms=round(data_ms),
                         ack_ms=round(ack_ms))
        if dec.trigger not in ("lock_in", "late_exit", "profit_take"):
            return                                # model-driven exits aren't a reaction to a price change
        self._latency = (self._latency + [send_ms])[-20:]
        limit = float(self.xc.get("latency_alert_ms", 1000))
        if len(self._latency) >= 5:
            med = sorted(self._latency)[len(self._latency) // 2]
            if med > limit and time.time() - self._latency_alerted > 3600:
                self._latency_alerted = time.time()
                self._alert("Slow exits", f"median price->sell order {med:.0f} ms over the last "
                            f"{len(self._latency)} exits (limit {limit:.0f} ms)", "warn", "error")

    async def _poll_exit(self, p: dict, now: float) -> None:
        o = await self.broker.poll(p["exit_order"])
        self.store.order(o, p["id"], "exit")
        timeout = float(self.xc.get("exit_order_timeout_s", 3))
        if not o.done and now - o.created < timeout:
            return
        if not o.done:
            o = await self.broker.cancel(o.order_id)
            self.store.order(o, p["id"], "exit")
            self.store.event("cancel", f"exit order {o.order_id} canceled (filled {o.filled:.2f}/{o.size:.2f})",
                             position=p["id"])
        sold = (p["exit_shares"] or 0) + o.filled
        proceeds = (p["exit_proceeds"] or 0) + o.notional
        fee = (p["exit_fee"] or 0) + o.fee
        self.store.update_position(p["id"], exit_shares=sold, exit_proceeds=proceeds, exit_fee=fee,
                                   exit_price=proceeds / sold if sold else None, exit_order=None)
        if o.filled:
            self.store.event("fill", f"SOLD {p['side']} {o.filled:.2f} @ {o.avg_price:.3f} fee {o.fee:.4f}",
                             position=p["id"], order=o.order_id)
            left = p["shares"] - sold
            rest = "all sold" if left <= 1e-6 else f"{left:g} left"
            self._alert(f"Sold {p['duration']} {p['side']} {o.filled:g} @ {o.avg_price:.2f}",
                        f"fee ${o.fee:.2f}, {rest} ({p.get('exit_reason') or ''})")
        if sold >= p["shares"] - 1e-6:          # exact: a leftover 0.01 contract is still a position
            self._close(self.store.position(p["id"]), payout=0.0, now=now)
        else:
            self.store.update_position(p["id"], status="open")   # retry the rest next tick

    async def _resolve(self, p: dict, now: float) -> None:
        if p["status"] != "resolving":
            self.store.update_position(p["id"], status="resolving")
            self.store.event("resolve", f"holding {p['side']} through close of {p['slug']}", position=p["id"])
        per_share = self.module.settlement_per_share(p)
        if per_share is None:
            return                                   # module keeps polling the official result
        left = p["shares"] - (p["exit_shares"] or 0)
        payout = left * per_share
        if self.mode == "live" and per_share > 0 and hasattr(self.broker, "redeem"):
            try:
                await self.broker.redeem(p["condition_id"])
                self.store.event("redeem", f"redeemed {left:.2f} winning shares", position=p["id"])
            except Exception as e:  # noqa: BLE001
                self.store.event("error", f"redeem failed (redeem in the Polymarket app): {e}", "ERROR",
                                 position=p["id"])
        label = self.module.outcome_label(p) if hasattr(self.module, "outcome_label") else \
            f"settled at {per_share:.2f}/share"
        self._close(self.store.position(p["id"]), payout=payout, now=now,
                    reason=label, trigger=p.get("exit_trigger") or "resolution")

    def _close(self, p: dict, payout: float, now: float, reason: str | None = None,
               trigger: str | None = None) -> None:
        basis = (p["cost"] or 0) + (p["entry_fee"] or 0)
        pnl = (p["exit_proceeds"] or 0) - (p["exit_fee"] or 0) + payout - basis
        kw = {"status": "closed", "payout": payout, "pnl": pnl, "won": int(pnl > 0), "closed_ts": now}
        if reason:
            prior = p.get("exit_reason") if p.get("exit_shares") else None
            kw["exit_reason"] = f"{prior}; then {reason}" if prior else reason
        if trigger:
            kw["exit_trigger"] = trigger
        self.store.update_position(p["id"], **kw)
        self.last_close[p["slug"]] = now
        self.store.event("close", f"{p['side']} {p['slug']} P&L {pnl:+.4f}", position=p["id"], pnl=pnl)
        for msg in self.risk.record_close(pnl, now):
            self.store.event("risk", msg, "WARNING")
            self._alert("Risk limit", msg, "warn", "risk")
        self._save()
        self._self_check(p)
        self._alert(f"Closed {p['duration']} {p['side']}: {pnl:+.2f}",
                    f"{kw.get('exit_reason') or p.get('exit_reason') or ''}. Today {self.risk.state.day_pnl:+.2f}.")
        self._rules_report(p)

    def _rules_report(self, p: dict) -> None:
        """After rules_report_after (50) trades on the current rules: compare against the old rules once."""
        if p.get("rules") != RULES_VERSION:
            return
        n_after = int(self.tc.get("rules_report_after", 50))
        key = f"rules_report:{self.mode}:{RULES_VERSION}"
        if self.store.get_state(key):
            return
        n = self.store.db.query("SELECT COUNT(*) n FROM bot_positions WHERE mode=? AND rules=? AND status='closed'",
                                (self.mode, RULES_VERSION))[0]["n"]
        if n < n_after:
            return
        try:
            from ..execution_report import analyse, compare_text
            text = compare_text(analyse(self.store.db, mode=self.mode, days=365))
        except Exception as e:  # noqa: BLE001
            log.exception("rules comparison failed")
            text = f"comparison failed: {e}"
        self.store.set_state(key, time.time())
        self.store.event("rules_report", text, "WARNING")
        self._alert(f"{n} trades on the new rules: old vs new", text, "info", "summary")

    @staticmethod
    def _strategy_stats(closed: list[dict]) -> dict:
        from .summary import strategy_stats
        out = {}
        for key in sorted({r.get("strategy") or "legacy" for r in closed} | {"fair_value"}):
            out[key] = strategy_stats([r for r in closed if (r.get("strategy") or "legacy") == key])
        return out

    def _self_check(self, p: dict) -> None:
        """PART 6: after 100 fair-value trades (then every 25), pause if calibration is poor or P&L < 0."""
        if p.get("strategy") != "fair_value":
            return
        from .summary import self_check
        sc = (self.tc.get("self_check", {}) or {})
        if sc.get("enabled", True) in (False, "off"):
            return
        rows = [r for r in self.store.closed_positions() if r.get("strategy") == "fair_value"]
        res = self_check(rows, after=int(sc.get("after_trades", 100)), every=int(sc.get("every", 25)),
                         max_cal_error=float(sc.get("max_calibration_error", 0.10)))
        if res is None:
            return
        st = res["stats"]
        report = (f"{res['n']} trades, P&L {st['pnl']:+.2f}, win rate {st['win_rate'] or 0:.0%} vs break-even "
                  f"{(st['break_even_win_rate'] or 0):.0%}, calibration error "
                  f"{(st['calibration']['error'] or 0):.1%} over {st['calibration']['n']} settled trades")
        self.store.event("self_check", report, "WARNING", ok=res["ok"], problems=res["problems"])
        if res["ok"]:
            self._alert("Self-check passed", report, "info", "summary")
        else:
            self._hard_pause("SELF-CHECK FAILED: " + "; ".join(res["problems"]))
            self._alert("Self-check FAILED - trading paused", report + ". " + "; ".join(res["problems"]),
                        "warn", "risk")

    # ------------------------------------------------------------ snapshot
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        positions = []
        for p in self.store.active_positions():
            m = self.module.market(p["slug"])
            end = m.hold_phase_from if m else None
            positions.append({**p, "seconds_left": round(end - now, 1) if end else None})
        closed = self.store.closed_positions()
        paper_n = len(self.store.counted_paper_trades(self.paper_since))
        day_start = time.mktime(time.strptime(local_day(now), "%Y-%m-%d"))
        return {
            "module": self.module.name,
            "mode": self.mode, "risk": self.risk.summary(now), "status_note": self.status_note,
            "balance": self.last_balance, "positions": positions,
            "trades": list(reversed(closed[-50:])),
            "stats_all": bot_stats(closed), "stats_today": bot_stats([c for c in closed
                                                                       if c["closed_ts"] >= day_start]),
            "paper_trades_done": paper_n,
            "paper_trades_required": self.min_paper,
            "strategy_stats": self._strategy_stats(closed),
            "paper_count_from": self.paper_since,
            "settings": {"stake": self.stake_now(), "stake_pct": self.ec.get("stake_pct"),
                         "order_style": self.ec.get("order_style", "maker"),
                         "take_profit_pct": float(self.xc.get("take_profit_pct", 0.08)),
                         "stop_loss_pct": float(self.xc.get("stop_loss_pct", 0.10))},
        }
