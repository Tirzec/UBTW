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
import logging
import time

from ..ingest.discovery import DURATION_SECONDS
from .broker import BrokerError, Maintenance, OrderState
from .risk import RiskManager, RiskState, local_day
from .rules import evaluate_bot_exit, plan_entry, round_price, trigger_levels, value_position
from .store import TradeStore, bot_stats

log = logging.getLogger("trader")

ACTIVE = ("opening", "open", "closing", "resolving")


class AutoTrader:
    def __init__(self, engine, cfg: dict, broker, store: TradeStore):
        self.engine = engine
        self.cfg = cfg
        self.tc = cfg.get("trading", {}) or {}
        self.ec = self.tc.get("entry", {}) or {}
        self.xc = self.tc.get("exits", {}) or {}
        self.broker = broker
        self.mode = broker.mode
        self.store = store
        saved = store.get_state("risk", {}) or {}
        st = RiskState(**{k: v for k, v in saved.items() if k in RiskState.__dataclass_fields__})
        self.risk = RiskManager(self.tc.get("risk", {}) or {}, st)
        self._recompute_day()
        self.orders: dict[str, OrderState] = {}
        self.last_close: dict[str, float] = {}
        self.status_note = ""
        self.last_balance: float | None = None
        self._last_balance_check = 0.0
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

    def _save(self) -> None:
        self.store.set_state("risk", {k: v for k, v in self.risk.state.__dict__.items()
                                      if k != "api_errors"})

    def _alert(self, title: str, body: str = "", level: str = "info", kind: str = "trade") -> None:
        self.engine.alert(kind, f"[{self.mode.upper()}] {title}", body, level)

    # ------------------------------------------------------------ control
    async def kill(self, reason: str = "STOP pressed") -> int:
        """Kill switch: cancel every open order and halt all trading (entries AND exits)."""
        self.risk.halt(f"KILL SWITCH: {reason}")
        self._save()
        n = 0
        try:
            n = await self.broker.cancel_all()
        except Exception as e:  # noqa: BLE001
            self.store.event("error", f"cancel_all failed during kill: {e}", "ERROR")
        for p in self.store.active_positions():
            if p["status"] == "opening":
                await self._finish_entry(p)
            elif p["status"] == "closing":
                self.store.update_position(p["id"], status="open", exit_order=None)
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
            for col, purpose in (("entry_order", "entry"), ("exit_order", "exit")):
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

    async def start(self) -> None:
        await self.recover()
        self.store.event("start", f"auto-trader started in {self.mode.upper()} mode", "WARNING",
                         risk=self.risk.summary())
        self._task = asyncio.create_task(self._loop(), name="autotrader")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.broker.close()

    async def _loop(self) -> None:
        interval = float(self.xc.get("check_interval_s", 1))
        while True:
            t0 = time.time()
            try:
                await self.tick(t0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("auto-trader tick failed")
                self._api_error("tick", e)
            await asyncio.sleep(max(0.1, interval - (time.time() - t0)))

    async def tick(self, now: float) -> None:
        # CLI kill switch / resume requests arrive through the DB
        req = self.store.get_state("kill_requested")
        if req and not (self.risk.state.halted and self.risk.state.halt_reason.startswith("KILL")):
            await self.kill("STOP from command line")
        if self.store.get_state("resume_requested"):
            self.store.set_state("resume_requested", None)
            self.resume()
        await self._housekeeping(now)
        if self.risk.exits_allowed():
            for p in self.store.active_positions():
                try:
                    await self._manage(p, now)
                except Maintenance as m:
                    self.risk.state.maintenance_until = now + m.retry_after
                except BrokerError as e:
                    self._api_error(f"position {p['id']}", e)
        for dur in self.engine.durations:
            try:
                await self._maybe_enter(dur, now)
            except Maintenance as m:
                self.risk.state.maintenance_until = now + m.retry_after
                self.store.event("maintenance", str(m), "WARNING")
            except BrokerError as e:
                self._api_error(f"entry {dur}", e)
        self._save()

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

    async def _maybe_enter(self, dur: str, now: float) -> None:
        ev = self.engine.evals.get(dur)
        w = self.engine.discovery.current(dur, now)
        if ev is None or ev.window.slug != w.slug or ev.decision is None or ev.p_up is None:
            return
        d = ev.decision
        if d.action not in ("UP", "DOWN"):
            return                                   # existing signal logic says NO TRADE
        left = w.end - now
        late = float(self.ec.get(f"no_entry_last_s_{dur}", 60 if dur == "5m" else 120))
        if left < late and (d.edge or 0) < float(self.ec.get("late_entry_min_edge", 0.12)):
            return
        stake = min(float(self.ec.get("stake_usd", 5)), self.risk.max_stake)
        blockers = self.risk.entry_blockers(now=now, stake=stake, open_exposure=self._exposure(),
                                            window_busy=self._window_busy(w.slug, now),
                                            degraded=self.engine.feeds.quality.state.degraded)
        if blockers:
            self.status_note = f"{dur} {d.action} signal skipped: {blockers[0]}"
            return
        side = d.action
        token = w.token_up if side == "UP" else w.token_down
        book = self.engine.clob.store.get(token) or {"bids": [], "asks": []}
        plan = plan_entry(best_bid=book["bids"][0][0] if book["bids"] else None,
                          best_ask=book["asks"][0][0] if book["asks"] else None, stake_usd=stake,
                          tick=w.tick_size, min_size=w.min_size,
                          style=self.ec.get("order_style", "maker"), fee_schedule=w.fee_schedule)
        if not plan.ok:
            self.status_note = f"{dur} {side} signal skipped: {plan.reason}"
            return
        p_side = ev.p_up if side == "UP" else 1 - ev.p_up
        pid = self.store.new_position(slug=w.slug, duration=dur, side=side, token=token,
                                      condition_id=w.condition_id, status="opening",
                                      entry_price=plan.price, p_model=p_side, edge=d.edge,
                                      opened_ts=now)
        o = await self.broker.place_limit(token=token, side="BUY", price=plan.price, size=plan.shares,
                                          post_only=plan.post_only)
        self.store.order(o, pid, "entry")
        self.store.update_position(pid, entry_order=o.order_id)
        self.store.event("order", f"BUY {side} {plan.shares:.2f} @ {plan.price:.2f} "
                         f"({'post-only' if plan.post_only else 'limit'}) {w.slug}", position=pid,
                         order=o.order_id, status=o.status, edge=round(d.edge, 4), p=round(p_side, 4))
        if o.status == "rejected":
            self.store.update_position(pid, status="void", exit_reason=f"entry rejected: {o.error}",
                                       closed_ts=now)
            if o.error.startswith("post_only_would_cross"):
                return                               # book moved; try again next tick
            raise BrokerError(f"entry rejected: {o.error}")
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
        if o.filled <= 0:
            self.store.update_position(p["id"], status="void", exit_reason="entry not filled in time",
                                       closed_ts=time.time())
            return
        w = self.engine.discovery.windows.get(p["slug"])
        tp, sl = trigger_levels(shares=o.filled, cost_basis=o.notional + o.fee,
                                take_profit_pct=float(self.xc.get("take_profit_pct", 0.08)),
                                stop_loss_pct=float(self.xc.get("stop_loss_pct", 0.10)),
                                fee_schedule=w.fee_schedule if w else None)
        self.store.update_position(p["id"], status="open", shares=o.filled, cost=o.notional,
                                   entry_fee=o.fee, entry_price=round(o.avg_price, 6), filled_ts=time.time(),
                                   tp_price=tp, sl_price=sl)
        self.store.event("fill", f"BOUGHT {p['side']} {o.filled:.2f} @ {o.avg_price:.3f} fee {o.fee:.4f}",
                         position=p["id"], order=o.order_id)
        self._alert(f"Bought {p['duration']} {p['side']} @ {o.avg_price:.2f}",
                    f"{o.filled:.1f} shares, ${o.notional:.2f}. TP bid ≥{tp:.2f}, SL bid ≤{sl:.2f}.")

    # ------------------------------------------------------------ exits
    async def _manage(self, p: dict, now: float) -> None:
        w = self.engine.discovery.windows.get(p["slug"])
        if p["status"] == "opening":
            o = await self.broker.poll(p["entry_order"])
            self.store.order(o, p["id"], "entry")
            timeout = float(self.ec.get("order_timeout_s", 8))
            if o.filled >= o.size - 1e-9 or now - o.created >= timeout or (w and now >= w.end - 5):
                await self._finish_entry(p)
            return
        if p["status"] == "closing":
            await self._poll_exit(p, now)
            return
        if p["status"] == "resolving" or (w and now >= w.end):
            await self._resolve(p, w, now)
            return
        await self._evaluate_exit(p, w, now)

    def _fair(self, p: dict) -> tuple[float | None, bool]:
        ev = self.engine.evals.get(p["duration"])
        ok = not self.engine.feeds.quality.state.degraded
        if ev is None or ev.window.slug != p["slug"] or ev.p_up is None:
            return None, False
        return (ev.p_up if p["side"] == "UP" else 1 - ev.p_up), ok

    async def _evaluate_exit(self, p: dict, w, now: float) -> None:
        book = self.engine.clob.store.get(p["token"]) or {"bids": [], "asks": []}
        shares = p["shares"] - (p["exit_shares"] or 0)
        basis = (p["cost"] + p["entry_fee"]) * (shares / p["shares"]) if p["shares"] else 0
        val = value_position(shares=shares, cost_basis=basis, bids=book["bids"],
                             fee_schedule=w.fee_schedule if w else None)
        fair, ok = self._fair(p)
        dur = p["duration"]
        dec = evaluate_bot_exit(
            val=val, fair=fair, seconds_left=(w.end - now) if w else 0, entry_price=p["entry_price"],
            take_profit_pct=float(self.xc.get("take_profit_pct", 0.08)),
            stop_loss_pct=float(self.xc.get("stop_loss_pct", 0.10)),
            flip_threshold=float(self.xc.get("flip_threshold", 0.5)),
            time_exit_s=float(self.xc.get(f"time_exit_s_{dur}", 20 if dur == "5m" else 30)),
            hold_min_ev=float(self.xc.get("hold_min_ev", 0.02)), model_ok=ok,
            seconds_since_fill=now - (p["filled_ts"] or now),
            stop_loss_grace_s=float(self.xc.get("stop_loss_grace_s", 0)),
            hard_stop_pct=float(self.xc.get("hard_stop_pct", 0.25)))
        self.store.update_position(p["id"], last_value=val.net, last_pnl=val.pnl,
                                   last_note=dec.reason[:200])
        if dec.action != "SELL" or not book["bids"]:
            return
        tick = w.tick_size if w else 0.01
        floor = round_price(max(tick, book["bids"][0][0] - int(self.xc.get("exit_slippage_ticks", 2)) * tick), tick)
        o = await self.broker.place_limit(token=p["token"], side="SELL", price=floor, size=round(shares, 2),
                                          post_only=False)
        self.store.order(o, p["id"], "exit")
        self.store.update_position(p["id"], status="closing", exit_order=o.order_id,
                                   exit_reason=dec.reason, exit_trigger=dec.trigger)
        self.store.event("order", f"SELL {p['side']} {shares:.2f} @ >= {floor:.2f}: {dec.reason}",
                         position=p["id"], order=o.order_id, status=o.status)
        if o.status == "rejected":
            self.store.update_position(p["id"], status="open", exit_order=None)
            raise BrokerError(f"exit rejected: {o.error}")
        await self._poll_exit(self.store.position(p["id"]), now)

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
        if sold >= p["shares"] - 0.01:
            self._close(self.store.position(p["id"]), payout=0.0, now=now)
        else:
            self.store.update_position(p["id"], status="open")   # retry the rest next tick

    async def _resolve(self, p: dict, w, now: float) -> None:
        if p["status"] != "resolving":
            self.store.update_position(p["id"], status="resolving")
            self.store.event("resolve", f"holding {p['side']} through close of {p['slug']}", position=p["id"])
        if w is None or w.outcome is None:
            return                                   # discovery refreshes until Gamma publishes it
        left = p["shares"] - (p["exit_shares"] or 0)
        won = w.outcome == p["side"]
        payout = left * (1.0 if won else 0.0)
        if self.mode == "live" and won and hasattr(self.broker, "redeem"):
            try:
                await self.broker.redeem(p["condition_id"])
                self.store.event("redeem", f"redeemed {left:.2f} winning shares", position=p["id"])
            except Exception as e:  # noqa: BLE001
                self.store.event("error", f"redeem failed (redeem in the Polymarket app): {e}", "ERROR",
                                 position=p["id"])
        self._close(self.store.position(p["id"]), payout=payout, now=now,
                    reason=f"resolved {w.outcome}", trigger=p.get("exit_trigger") or "resolution")

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
        self._alert(f"Closed {p['duration']} {p['side']}: {pnl:+.2f}",
                    f"{kw.get('exit_reason') or p.get('exit_reason') or ''}. Today {self.risk.state.day_pnl:+.2f}.")

    # ------------------------------------------------------------ snapshot
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        positions = []
        for p in self.store.active_positions():
            w = self.engine.discovery.windows.get(p["slug"])
            positions.append({**p, "seconds_left": round(w.end - now, 1) if w else None})
        closed = self.store.closed_positions()
        paper_n = len(self.store.closed_positions(mode="paper"))
        day_start = time.mktime(time.strptime(local_day(now), "%Y-%m-%d"))
        return {
            "mode": self.mode, "risk": self.risk.summary(now), "status_note": self.status_note,
            "balance": self.last_balance, "positions": positions,
            "trades": list(reversed(closed[-50:])),
            "stats_all": bot_stats(closed), "stats_today": bot_stats([c for c in closed
                                                                       if c["closed_ts"] >= day_start]),
            "paper_trades_done": paper_n,
            "paper_trades_required": int(self.tc.get("min_paper_trades_for_live", 300)),
            "settings": {"stake": min(float(self.ec.get("stake_usd", 5)), self.risk.max_stake),
                         "order_style": self.ec.get("order_style", "maker"),
                         "take_profit_pct": float(self.xc.get("take_profit_pct", 0.08)),
                         "stop_loss_pct": float(self.xc.get("stop_loss_pct", 0.10))},
        }
