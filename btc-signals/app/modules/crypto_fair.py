"""crypto_fair: BTC Up/Down strategy built only on the settlement math.

PART 1  Fair P(UP) every second from the exact settlement rule (app/settlement.py): the final value is
        the mean of 60 one-second BRTI prices before the close (rounded to cents, >= wins). The public
        BRTI series is itself that 60 s average (lags ~30 s); live spot (Coinbase/Kraken median) +
        basis is the raw-price early signal. Blocks on stale/disagreeing feeds and extreme volatility.
PART 2  Entries: 15m preferred; 5m only at 2x the edge threshold; none in the first third; edge =
        fair - ask - taker fee >= threshold; exit depth required; ask within [0.05, 0.95]; one position
        per window; limit order at or slightly below the ask, canceled after 10 s.
PART 3  Exits every second: SELL if the bid (after fees) is above fair; otherwise HOLD to resolution.
        Emergency exit only on data failure or a sharp, high-confidence flip. Never into an empty book.
PART 4  UP+DOWN arbitrage watcher. On Polymarket US both sides are one order book (Down = short Up), so
        UP ask + DOWN ask = 1 + spread > 1 always; the watcher records the best sum seen and alerts if
        a real opportunity ever appears. It never trades on its own.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Optional

from ..fees import fee_per_share
from ..settlement import FairEstimator, FairValue
from ..trading.rules import ExitDecision
from .base import MarketInfo, Reason, Signal, StrategyModule


class CryptoFairModule(StrategyModule):
    name = "crypto_updown"          # same module slot: risk state, stats, kill switch, dashboard tab

    def __init__(self, engine, cfg: dict):
        self.engine = engine
        tc = cfg.get("trading", {}) or {}
        self.ec = tc.get("entry", {}) or {}
        self.fc = cfg.get("fair_value", {}) or {}
        f = self.fc
        self.est = FairEstimator(sigma_step=int(f.get("sigma_step_s", 10)),
                                 sigma_lookback=int(f.get("sigma_lookback_s", 1800)),
                                 basis_halflife=float(f.get("basis_halflife_s", 600)),
                                 dist=f.get("dist", "normal"), nu=float(f.get("nu", 4)),
                                 vol_mult=float(f.get("vol_mult", 1.0)))
        self.started = time.time()
        self.fair_now: dict[str, FairValue] = {}             # slug -> latest fair value
        self.fair_hist: dict[str, deque] = {}                # slug -> (ts, p_up) last ~60 s
        self.sigma_hist: deque = deque(maxlen=720)           # (ts, sigma) every 30 s, 6 h
        self.problems: list[str] = []
        self.last_tick = 0
        self.arb: dict[str, dict] = {}
        self.notes: dict[str, str] = {}

    # ------------------------------------------------------------ data
    def _spot_names(self) -> list[str]:
        return [fd.name for fd in self.engine.feeds.spot_feeds]

    def _close(self, name: str, sec: int, max_back: int = 5) -> Optional[float]:
        bars = self.engine.feeds.bars.get(name)
        if bars is None or sec < self.started:
            return None                              # ignore pre-loaded (smoothed) history
        for k in range(sec, sec - max_back - 1, -1):
            b = bars._bars.get(k)
            if b is not None and k >= self.started:
                return b[3]
        return None

    def spot_at(self, sec: int) -> Optional[float]:
        vals = [v for v in (self._close(n, sec) for n in self._spot_names()) if v is not None]
        return statistics.median(vals) if vals else None

    def _x_series(self):
        name = self.engine.feeds.active_reference or (self.engine.feeds.reference_feeds[0].name
                                                        if self.engine.feeds.reference_feeds else None)
        return self.engine.feeds.ref_spot.get(name) if name else None

    def _update(self, now: float) -> None:
        """Once per second: basis, data checks, fair values."""
        sec = int(now) - 1                                   # last complete second
        if sec <= self.last_tick:
            return
        self.last_tick = sec
        xs = self._x_series()
        x = xs.get(sec) if xs else None
        if x is not None:
            vals = [self.spot_at(s) for s in range(sec - 59, sec + 1)]
            if None not in vals:
                self.est.update_basis(sec, x, sum(vals) / 60)
        self.problems = self._data_problems(now, xs)
        self.fair_now = {}
        for dur in self.engine.durations:
            w = self.engine.discovery.current(dur, now)
            if w.gamma_ptb is None:
                continue
            fv = self.est.compute(now=sec, end=w.end, price_to_beat=w.gamma_ptb,
                                  x_at=(xs.get if xs else (lambda s: None)), spot_at=self.spot_at,
                                  x_latest_sec=xs.last_sec if xs else None)
            if fv is not None:
                self.fair_now[w.slug] = fv
                h = self.fair_hist.setdefault(w.slug, deque(maxlen=90))
                h.append((now, fv.p_up))
        if sec % 30 == 0 and self.fair_now:
            self.sigma_hist.append((now, next(iter(self.fair_now.values())).sigma))
        self._arb_watch(now)

    def _data_problems(self, now: float, xs) -> list[str]:
        f, out = self.fc, []
        x_age = now - xs.last_sec if (xs and xs.last_sec) else 1e9
        if x_age > float(f.get("max_index_age_s", 6)):
            out.append(f"BRTI feed stale ({x_age:.0f}s)")
        fresh = {}
        for n in self._spot_names():
            v = self._close(n, int(now) - 1, max_back=int(f.get("max_spot_age_s", 5)))
            if v is not None:
                fresh[n] = v
        if not fresh:
            out.append("no fresh spot feed")
        elif len(fresh) >= 2:
            lo, hi = min(fresh.values()), max(fresh.values())
            bps = (hi - lo) / lo * 1e4
            if bps > float(f.get("max_spot_disagree_bps", 15)):
                out.append(f"spot feeds disagree by {bps:.0f} bps")
        if now - self.started < float(f.get("warmup_s", 600)):
            out.append(f"warming up ({self.started + float(f.get('warmup_s', 600)) - now:.0f}s left)")
        if self.est.basis is None:
            out.append("basis not known yet")
        sig = [s for _, s in self.sigma_hist]
        cur = next(iter(self.fair_now.values())).sigma if self.fair_now else None
        if cur is not None and len(sig) >= 20:
            med = statistics.median(sig)
            if cur > float(f.get("max_vol_ratio", 3.0)) * med:
                out.append(f"volatility extreme ({cur / med:.1f}x the recent median)")
        return out

    # ------------------------------------------------------------ PART 4: arbitrage watcher
    def _arb_watch(self, now: float) -> None:
        for dur in self.engine.durations:
            w = self.engine.discovery.current(dur, now)
            up, dn = self.book(w.token_up), self.book(w.token_down)
            if not up or not dn or not up["asks"] or not dn["asks"]:
                continue
            a_up, a_dn = up["asks"][0][0], dn["asks"][0][0]
            sched = w.fee_schedule
            total = a_up + a_dn + fee_per_share(a_up, sched) + fee_per_share(a_dn, sched)
            st = self.arb.setdefault(dur, {"checks": 0, "best_sum": None, "opportunities": 0})
            st["checks"] += 1
            st["best_sum"] = total if st["best_sum"] is None else min(st["best_sum"], total)
            if total <= 1.0 - float(self.fc.get("arb_min_profit", 0.01)):
                st["opportunities"] += 1
                self.engine.alert("risk", f"{dur} UP+DOWN priced below $1 ({total:.3f})",
                                  "Logged only: on Polymarket US both sides are one order book.", "info")

    # ------------------------------------------------------------ PART 2: entries
    def signals(self, now: float) -> list[Signal]:
        self._update(now)
        if self.problems:
            return []
        out = []
        f = self.fc
        thr = float(f.get("min_edge", 0.05))
        for dur in sorted(self.engine.durations, key=lambda d: 0 if d == "15m" else 1):   # prefer 15m
            w = self.engine.discovery.current(dur, now)
            fv = self.fair_now.get(w.slug)
            need = thr * (float(f.get("five_min_multiplier", 2.0)) if dur == "5m" else 1.0)
            L = w.end - w.start
            if fv is None or not w.accepting_orders or (w.rules is not None and not w.rules.ok):
                continue
            if now < w.start + L / 3 or now > w.end - float(f.get("no_entry_last_s", 5)):
                self.notes[dur] = "first third of the window" if now < w.start + L / 3 else "window ending"
                continue
            best = None
            for side, tok in (("UP", w.token_up), ("DOWN", w.token_down)):
                bk = self.book(tok)
                if not bk or not bk["asks"] or not bk["bids"]:
                    continue
                if now - (bk.get("updated") or 0) > float(f.get("max_book_age_s", 5)):
                    continue
                ask, ask_sz = bk["asks"][0]
                bid, bid_sz = bk["bids"][0]
                if not float(f.get("min_price", 0.05)) <= ask <= float(f.get("max_price", 0.95)):
                    continue
                p = fv.p_side(side)
                edge = p - ask - fee_per_share(ask, w.fee_schedule)
                stake = float(self.ec.get("stake_usd", 1))
                shares = stake / ask
                depth_bid = sum(s for px, s in bk["bids"] if px >= bid - 3 * w.tick_size)
                if edge < need:
                    continue
                if depth_bid < shares or ask_sz < min(shares, 1):
                    self.notes[dur] = f"{side} edge {edge:+.3f} but book too thin to exit"
                    continue
                limit = ask if edge >= 2 * need else max(round(ask - w.tick_size, 4), round(bid + w.tick_size, 4))
                limit = min(limit, ask)
                if best is None or edge > best[0]:
                    best = (edge, side, tok, p, ask, limit)
            if best is None:
                continue
            edge, side, tok, p, ask, limit = best
            out.append(Signal(module=self.name, market_id=w.slug, token=tok, side=side, fair_prob=p,
                              confidence=1.0, edge=edge, label=dur, group=w.slug, bet_type="updown",
                              expires_at=w.end,
                              reasons=[Reason(f"fair {p:.3f} vs ask {ask:.2f} (fee incl.): edge {edge:+.3f}",
                                              "settlement model", edge, now),
                                       Reason(f"{w.end - now:.0f}s left, sigma ${fv.sigma:.2f}/s^0.5, "
                                              f"{fv.locked}/60 prices locked", "settlement model", 0, now)],
                              extra={"limit": limit, "time_left": w.end - now, "strategy": "fair_value"}))
        return out

    def entry_blockers(self, signal: Signal, active: list[dict], now: float) -> list[str]:
        return list(self.problems)

    def degraded(self) -> bool:
        return bool(self.problems)

    # ------------------------------------------------------------ markets
    def market(self, slug: str) -> Optional[MarketInfo]:
        w = self.engine.discovery.windows.get(slug)
        if w is None:
            return None
        return MarketInfo(slug=slug, tick_size=w.tick_size, min_size=w.min_size,
                          size_step=getattr(w, "size_step", 0.01), fee_schedule=w.fee_schedule,
                          condition_id=w.condition_id, label=w.duration,
                          entry_deadline=w.end - float(self.fc.get("no_entry_last_s", 5)),
                          hold_phase_from=w.end, seconds_left=w.end - time.time())

    def book(self, token: str) -> Optional[dict]:
        return self.engine.clob.store.get(token)

    def fair(self, p: dict) -> tuple[Optional[float], bool]:
        fv = self.fair_now.get(p["slug"])
        if fv is None:
            return None, False
        return fv.p_side(p["side"]), not self.problems

    # ------------------------------------------------------------ PART 3: exits
    def exit_decision(self, p: dict, val, now: float) -> ExitDecision:
        if val.shares_sellable <= 0:
            return ExitDecision("HOLD", "no bids: never sell into an empty book")
        fair, ok = self.fair(p)
        if fair is None or not ok:
            why = "; ".join(self.problems) or "no fair value"
            return ExitDecision("SELL", f"emergency exit: data failure ({why})", "data_failure")
        net = val.net_per_share
        if net > fair:
            return ExitDecision("SELL", f"bid nets {net:.3f} > fair {fair:.3f}: someone is overpaying",
                                "bid_above_fair")
        h = self.fair_hist.get(p["slug"]) or []
        f = self.fc
        window = float(f.get("flip_window_s", 10))
        past = [pu for ts, pu in h if now - ts <= window]
        if past:
            first = past[0] if p["side"] == "UP" else 1 - past[0]
            if first - fair >= float(f.get("flip_drop", 0.35)) and fair <= float(f.get("flip_floor", 0.15)):
                return ExitDecision("SELL", f"sharp flip: fair {first:.2f} -> {fair:.2f} in {window:.0f}s",
                                    "sharp_flip")
        return ExitDecision("HOLD", f"fair {fair:.3f} > bid nets {net:.3f}: hold to resolution",
                            "hold_to_resolution")

    def settlement_per_share(self, p: dict) -> Optional[float]:
        w = self.engine.discovery.windows.get(p["slug"])
        if w is None or w.outcome is None:
            return None
        return 1.0 if w.outcome == p["side"] else 0.0

    def outcome_label(self, p: dict) -> str:
        w = self.engine.discovery.windows.get(p["slug"])
        return f"resolved {w.outcome}" if w and w.outcome else "resolved"

    def snapshot(self) -> dict:
        now = time.time()
        return {"strategy": "fair_value", "problems": self.problems, "basis": self.est.basis,
                "fair": {slug: {"p_up": round(fv.p_up, 4), "sigma": round(fv.sigma, 3), "locked": fv.locked,
                                "seconds_left": round(fv.seconds_left)} for slug, fv in self.fair_now.items()},
                "notes": self.notes, "arbitrage": self.arb, "uptime_s": round(now - self.started)}
