"""crypto_updown: the existing BTC Up/Down strategy behind the StrategyModule interface.
Behaviour is unchanged: signals come from the engine's per-window evaluation, exits use
evaluate_bot_exit with the trading.exits settings, settlement from the window outcome."""
from __future__ import annotations

import asyncio
import time

from typing import Optional

from ..trading.rules import evaluate_bot_exit
from .base import MarketInfo, Reason, Signal, StrategyModule


class CryptoUpDownModule(StrategyModule):
    name = "crypto_updown"

    def __init__(self, engine, cfg: dict):
        self.engine = engine
        tc = cfg.get("trading", {}) or {}
        self.ec = tc.get("entry", {}) or {}
        self.xc = tc.get("exits", {}) or {}
        self._settle_asked: dict[str, float] = {}
        self.notes: dict[str, str] = {}

    # ------------------------------------------------------------ entries
    def signals(self, now: float) -> list[Signal]:
        out = []
        for dur in self.engine.durations:
            ev = self.engine.evals.get(dur)
            w = self.engine.discovery.current(dur, now)
            if ev is None or ev.window.slug != w.slug or ev.decision is None or ev.p_up is None:
                continue
            d = ev.decision
            if d.action not in ("UP", "DOWN"):
                continue                                 # existing signal logic says NO TRADE
            left = w.end - now
            late = float(self.ec.get(f"no_entry_last_s_{dur}", 60 if dur == "5m" else 120))
            if left < late and (d.edge or 0) < float(self.ec.get("late_entry_min_edge", 0.12)):
                continue
            p_side = ev.p_up if d.action == "UP" else 1 - ev.p_up
            reasons = [Reason(x.get("text", ""), "model", x.get("contribution", 0.0), now)
                       for x in (getattr(ev, "drivers", None) or [])]
            out.append(Signal(module=self.name, market_id=w.slug,
                              token=w.token_up if d.action == "UP" else w.token_down,
                              side=d.action, fair_prob=p_side, confidence=1.0, edge=d.edge or 0.0,
                              reasons=reasons, group=w.slug, bet_type="updown", label=dur,
                              expires_at=w.end))
        return out

    def entry_blockers(self, signal: Signal, active: list[dict], now: float) -> list[str]:
        return []                                        # per-window rules live in the executor

    def degraded(self) -> bool:
        return bool(self.engine.feeds.quality.state.degraded)

    # ------------------------------------------------------------ markets
    def market(self, slug: str) -> Optional[MarketInfo]:
        w = self.engine.discovery.windows.get(slug)
        if w is None:
            # not tracked any more (e.g. the window ended while the bot was offline): build it from the slug
            # so the position still reaches resolution and settles
            from ..exit_report import _window
            dur, start, end = _window(slug)
            if dur is None:
                return None
            return MarketInfo(slug=slug, tick_size=0.01, min_size=0.01, size_step=0.01,
                              fee_schedule={"rate": 0.095, "exponent": 1, "takerOnly": True, "round_cents": True},
                              condition_id="", label=dur, entry_deadline=end - 5, hold_phase_from=end,
                              seconds_left=end - time.time())
        return MarketInfo(slug=slug, tick_size=w.tick_size, min_size=w.min_size, size_step=getattr(w, "size_step", 0.01),
                          fee_schedule=w.fee_schedule, condition_id=w.condition_id, label=w.duration,
                          entry_deadline=w.end - 5, hold_phase_from=w.end)

    def book(self, token: str) -> Optional[dict]:
        return self.engine.clob.store.get(token)

    def fair(self, p: dict) -> tuple[Optional[float], bool]:
        ev = self.engine.evals.get(p["duration"])
        ok = not self.engine.feeds.quality.state.degraded
        if ev is None or ev.window.slug != p["slug"] or ev.p_up is None:
            return None, False
        return (ev.p_up if p["side"] == "UP" else 1 - ev.p_up), ok

    def exit_decision(self, p: dict, val, now: float):
        w = self.engine.discovery.windows.get(p["slug"])
        fair, ok = self.fair(p)
        dur = p["duration"]
        x = self.xc
        return evaluate_bot_exit(
            val=val, fair=fair, seconds_left=(w.end - now) if w else 0, entry_price=p["entry_price"],
            take_profit_pct=float(x.get("take_profit_pct", 0.08)),
            stop_loss_pct=float(x.get("stop_loss_pct", 0.10)),
            flip_threshold=float(x.get("flip_threshold", 0.5)),
            time_exit_s=float(x.get(f"time_exit_s_{dur}", 20 if dur == "5m" else 30)),
            hold_min_ev=float(x.get("hold_min_ev", 0.02)), model_ok=ok,
            seconds_since_fill=now - (p["filled_ts"] or now),
            stop_loss_grace_s=float(x.get("stop_loss_grace_s", 0)),
            hard_stop_pct=float(x.get("hard_stop_pct", 0.25)))

    def settlement_per_share(self, p: dict) -> Optional[float]:
        w = self.engine.discovery.windows.get(p["slug"])
        if w is not None and w.outcome is not None:
            return 1.0 if w.outcome == p["side"] else 0.0
        # The window may no longer be tracked (e.g. after a restart): ask the exchange directly.
        now = time.time()
        if now - self._settle_asked.get(p["slug"], 0) > 15:
            self._settle_asked[p["slug"]] = now
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:                      # no running loop (unit tests)
                loop = None
            if loop is not None:
                loop.create_task(self._fetch_settlement(p["slug"]))
        return None

    async def _fetch_settlement(self, slug: str) -> None:
        from ..exit_report import _window
        dur, start, _ = _window(slug)
        if dur is None:
            return
        disc = self.engine.discovery
        w = disc.get(dur, start)
        if w.slug != slug:
            return
        try:
            await disc.refresh(w)
        except Exception as e:  # noqa: BLE001 - try again on the next check
            self.notes["settlement"] = f"{slug}: {type(e).__name__}"
            return
        if w.outcome is not None and hasattr(self.engine, "_on_settled"):
            self.engine._on_settled(w)                # records side_won for calibration

    def outcome_label(self, p: dict) -> str:
        w = self.engine.discovery.windows.get(p["slug"])
        return f"resolved {w.outcome}" if w and w.outcome else "resolved"
