"""The live engine: wires feeds -> features -> model -> signals/exits -> storage/UI."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .config import resolve_path
from .db import DB
from .exits import evaluate_exit
from .features import FeatureSet, compute_features
from .feeds.manager import FeedManager
from .feeds.quality import bps
from .ingest.calendar import EconCalendar, blackout_overlaps
from .ingest.derivs import Derivatives
from .ingest.discovery import DURATION_SECONDS, Discovery, Window
from .ingest.news import Headline, NewsFeed
from .ingest.polymarket_ws import ClobMarketStream
from .model import LogisticModel, top_drivers
from .notify import Notifier
from .paper import PaperTrader, paper_stats
from .signal import Decision, decide

log = logging.getLogger("engine")

WARMUP_SECONDS = 120
BOOK_STALE_S = 5          # seconds, measured from the exchange's own book timestamp (transactTime)


@dataclass
class WindowEval:
    window: Window
    p_up: float | None = None
    p_prior: float | None = None
    features: FeatureSet | None = None
    decision: Decision | None = None
    drivers: list[dict] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    ts: float = 0.0


def _mid(book: dict | None) -> float | None:
    if not book or not book["bids"] or not book["asks"]:
        return None
    return (book["bids"][0][0] + book["asks"][0][0]) / 2


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = DB(resolve_path(cfg, cfg.get("db_path", "data/signals.db")))
        self.http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-signals/1.0"})
        from .feeds.manager import venue_of
        self.venue = venue_of(cfg)
        self.feeds = FeedManager(cfg)
        if self.venue == "polymarket_us":
            from .ingest.us_markets import UsDiscovery
            self.discovery = UsDiscovery(cfg, self.http)
        else:
            self.discovery = Discovery(cfg, self.http)
        self.discovery.on_settled = self._on_settled
        if self.venue == "polymarket_us":
            from .ingest.us_markets import UsBookPoller
            self.clob = UsBookPoller(cfg, self.http)
        else:
            self.clob = ClobMarketStream(cfg, self.http)
        self.derivs = Derivatives(cfg, self.http)
        self.news = NewsFeed(cfg, on_new=self._on_headline)
        self.notifier = Notifier(cfg, self.http)
        self.calendar = EconCalendar(cfg)
        mc = cfg.get("model", {})
        self.models = {"5m": LogisticModel.load(resolve_path(cfg, mc.get("path_5m", "models/model_5m.json"))),
                       "15m": LogisticModel.load(resolve_path(cfg, mc.get("path_15m", "models/model_15m.json")))}
        pc = cfg.get("paper", {})
        self.paper = PaperTrader(self.db, pc.get("stake_usd", 50), pc.get("enabled", True))
        self.sc = cfg.get("signal", {})
        self.xc = cfg.get("exits", {})
        self.durations = list(self.discovery.durations)
        self.evals: dict[str, WindowEval] = {}
        self.entries: dict[str, int] = {}
        self.last_action: dict[str, str] = {}
        self.position_advice: dict[str, dict] = {}
        self.alerts: deque[dict] = deque(maxlen=100)
        self._alert_id = 0
        self._tasks: list[asyncio.Task] = []
        self._rest_refresh: dict[str, float] = {}
        self._last_pred: dict[str, float] = {}
        self._stats_cache: tuple[float, dict] = (0.0, {})
        self._started = time.time()
        self._was_degraded = None
        self.loop_lag_ms = 0.0
        self.trader = None
        self.traders: dict = {}
        self.sports = None
        self.global_risk = None
        tc = cfg.get("trading", {}) or {}
        self.trading_enabled = bool(tc.get("enabled", True))
        mode = "live" if cfg.get("_live_confirmed") else "paper"
        if tc.get("mode") == "live" and mode != "live":
            log.warning("trading.mode is live but the live checks were not passed - running PAPER")
        self.trade_mode = mode
        self.reconciler = None
        self.live_phase = None
        if mode == "live":
            from .trading.live_phase import apply_phase1
            self.live_phase = apply_phase1(cfg)          # hard phase-1 caps, before any trader exists
            log.warning("LIVE phase 1 limits in force: %s", self.live_phase)
            from .notify import LIVE_REQUIRED_KINDS
            self.notifier.kinds |= LIVE_REQUIRED_KINDS   # fills, limits, mismatches, kill, errors, summary
        if self.trading_enabled:
            from .trading.store import TradeStore, setup_trade_log
            setup_trade_log(resolve_path(cfg, tc.get("log_file", "data/trader.log")))
            self.trade_store = TradeStore(self.db, mode, self.venue)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.feeds.seed_history()
        await self.feeds.start()
        for name, coro in (("discovery", self.discovery.run()), ("clob", self.clob.run()),
                           ("derivs", self.derivs.run()), ("news", self.news.run()),
                           ("calendar", self.calendar.run()), ("signals", self._signal_loop()),
                           ("exits", self._exit_loop())):
            self._tasks.append(asyncio.create_task(self._guard(name, coro), name=name))
        if self.trading_enabled:
            await self._start_trader()

    def tick_for(self, token: str) -> float:
        for w in self.discovery.windows.values():
            if token in (w.token_up, w.token_down):
                return w.tick_size
        return 0.01

    def fee_schedule_for(self, token: str) -> dict:
        for w in self.discovery.windows.values():
            if token in (w.token_up, w.token_down):
                return w.fee_schedule
        return {}

    async def _start_trader(self) -> None:
        from .trading.broker import LiveBroker, PaperBroker
        from .trading.trader import AutoTrader
        if self.trade_mode == "live" and self.venue == "polymarket_us":
            from .trading.us_broker import UsLiveBroker
            creds = self.cfg["_live_confirmed"]
            broker = UsLiveBroker(creds["key_id"], creds["secret_key"], self.fee_schedule_for,
                                  base=self.cfg.get("polymarket_us", {}).get("api_url", "https://api.polymarket.us"),
                                  tick_for=self.tick_for)
        elif self.trade_mode == "live":
            creds = self.cfg["_live_confirmed"]
            broker = await LiveBroker.connect(creds["private_key"], creds["wallet"], self.fee_schedule_for)
        else:
            broker = PaperBroker(self.clob.store.get, self.clob.store.trades_since, self.fee_schedule_for,
                                 ioc_non_post_only=self.venue == "polymarket_us")
        from .trading.risk import GlobalRisk
        trades_today = None
        if self.trade_mode == "live":
            from .trading.live_phase import live_entries_today
            trades_today = lambda: live_entries_today(self.db, venue=self.venue)  # noqa: E731
        self.global_risk = GlobalRisk(self.cfg.get("risk_global", {}), trades_today_fn=trades_today)
        if self.trade_mode == "live" and self.venue == "polymarket_us":
            # load positions from the exchange FIRST; any mismatch halts before the first order
            from .trading.reconcile import Reconciler
            self.reconciler = Reconciler(self, broker, self.db,
                                         interval_s=float(self.cfg.get("trading", {}).get("reconcile_interval_s", 60)),
                                         venue=self.venue, bot_orders_fn=self._bot_open_orders)
            try:
                r = await self.reconciler.startup()
                log.warning("startup reconciliation: %s", "OK" if r.ok else "; ".join(r.mismatches))
            except Exception as e:  # noqa: BLE001 - can't verify the account: don't trade
                reason = f"RECONCILE: could not read positions from the exchange at startup ({e})"
                self.trade_store.set_state("kill_requested", {"ts": time.time(), "reason": reason})
                self.alert("risk", "Startup check failed - trading HALTED", reason, "warn")
        crypto_on = (self.cfg.get("modules", {}) or {}).get("crypto_updown", {}).get("enabled", True)
        self.traders = {}
        if crypto_on:
            self.trader = AutoTrader(self, self.cfg, broker, self.trade_store, global_risk=self.global_risk)
            self.traders["crypto_updown"] = self.trader
            await self.trader.start()
        await self._start_sports()
        if self.reconciler is not None:
            self._tasks.append(asyncio.create_task(self._guard("reconcile", self.reconciler.run()),
                                                   name="reconcile"))
        self._tasks.append(asyncio.create_task(self._guard("daily-summary", self._summary_loop()),
                                               name="daily-summary"))

    def daily_summary(self, date: str | None = None, now: float | None = None) -> tuple[str, str]:
        from .trading.summary import build_summary, format_summary
        return format_summary(build_summary(self.db, mode=self.trade_mode, venue=self.venue, date=date, now=now))

    async def _summary_loop(self) -> None:
        """Once a day at notifications.daily_summary_at (local time, default 23:55): push the summary."""
        at = str((self.cfg.get("notifications", {}) or {}).get("daily_summary_at", "23:55"))
        hh, mm = (int(x) for x in at.split(":"))
        while True:
            now = time.time()
            lt = time.localtime(now)
            day = time.strftime("%Y-%m-%d", lt)
            if (lt.tm_hour, lt.tm_min) >= (hh, mm) and self.trade_store.get_state("summary_sent") != day:
                title, body = self.daily_summary(now=now)
                self.alert("summary", title, body, "info")
                self.trade_store.set_state("summary_sent", day)
            await asyncio.sleep(30)

    def _bot_open_orders(self) -> dict:
        """{order id: market slug} of every order any trader currently holds open."""
        out = {}
        for t in getattr(self, "traders", {}).values():
            for oid, o in t.broker.orders.items():
                if not o.done:
                    out[oid] = o.token.rsplit(":", 1)[0]
        return out

    async def _start_sports(self) -> None:
        """Sports module: Polymarket US only. Live only after its own 200-paper-trade gate."""
        sc = self.cfg.get("sports", {}) or {}
        if not sc.get("enabled", False):
            return
        if self.venue != "polymarket_us":
            log.warning("sports module needs venue: polymarket_us; not started")
            return
        from .modules.sports.module import SportsModule
        from .trading.broker import PaperBroker
        from .trading.store import TradeStore
        from .trading.trader import AutoTrader
        need = int(sc.get("min_paper_trades_for_live", 200))
        from .trading.store import paper_count_from
        paper_done = len(TradeStore(self.db, "paper", self.venue, "sports").counted_paper_trades(
            paper_count_from(sc)))
        mode = "paper"
        if self.trade_mode == "live" and sc.get("live_enabled", False):
            if paper_done >= need:
                mode = "live"
            else:
                log.warning("sports stays in PAPER: %d/%d paper trades completed", paper_done, need)
        self.sports = SportsModule(self, self.cfg, self.http)
        store = TradeStore(self.db, mode, self.venue, "sports")
        self.sports.attach(store)
        fee_for = lambda tok: (self.sports.market_index.get(tok.rsplit(":", 1)[0]).fee_schedule  # noqa: E731
                               if self.sports.market_index.get(tok.rsplit(":", 1)[0]) else {})
        tick_for = lambda tok: (self.sports.market_index.get(tok.rsplit(":", 1)[0]).tick_size  # noqa: E731
                                if self.sports.market_index.get(tok.rsplit(":", 1)[0]) else 0.01)
        if mode == "live":
            from .trading.us_broker import UsLiveBroker
            creds = self.cfg["_live_confirmed"]
            broker = UsLiveBroker(creds["key_id"], creds["secret_key"], fee_for,
                                  base=self.cfg.get("polymarket_us", {}).get("api_url", "https://api.polymarket.us"),
                                  tick_for=tick_for)
        else:
            broker = PaperBroker(self.sports.books.get, self.sports.books.trades_since, fee_for,
                                 ioc_non_post_only=True)
        t = AutoTrader(self, self.cfg, broker, store, module=self.sports, exec_conf=sc,
                       global_risk=self.global_risk, min_paper_trades=need)
        self.traders["sports"] = t
        await self.sports.start()
        await t.start()

    async def kill_all(self, reason: str) -> int:
        n = 0
        for t in self.traders.values():
            n += await t.kill(reason)
        return n

    def resume_all(self) -> None:
        for t in self.traders.values():
            t.resume()

    async def _guard(self, name: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("task %s crashed", name)
            self.alert("error", f"Background task '{name}' crashed", f"{type(e).__name__}: {e}"[:300], "warn")

    async def stop(self) -> None:
        for t in getattr(self, "traders", {}).values():
            await t.stop()
        if getattr(self, "sports", None) is not None:
            await self.sports.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.feeds.stop()
        await self.http.aclose()

    # ------------------------------------------------------------ alerts
    def alert(self, kind: str, title: str, body: str = "", level: str = "info") -> None:
        self._alert_id += 1
        a = {"id": self._alert_id, "ts": time.time(), "kind": kind,
             "title": title, "body": body, "level": level}
        self.alerts.append(a)
        try:
            self.notifier.submit(a)
        except RuntimeError:      # no running event loop (e.g. in unit tests)
            pass

    def _on_headline(self, h: Headline) -> None:
        self.db.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?)",
                        (h.link, h.published, h.source, h.title, h.label, h.score,
                         h.confidence, json.dumps(h.hits)))

    def _on_settled(self, w: Window) -> None:
        n = self.paper.settle(w.slug, w.outcome)
        self.db.upsert_window(w)
        if n:
            self.alert("settled", f"{w.duration} window settled {w.outcome}",
                       f"{w.title}: final {w.final_price:,.2f} vs {w.gamma_ptb or 0:,.2f}")

    # ------------------------------------------------------------ price to beat
    def ensure_price_to_beat(self, w: Window, now: float) -> None:
        if w.gamma_ptb is not None:
            if w.price_to_beat is not None and w.ptb_source in ("chainlink", "synthetic", "prev_final"):
                w.ptb_check_bps = bps(w.price_to_beat, w.gamma_ptb)
                if abs(w.ptb_check_bps) > 1.0:
                    log.warning("price to beat mismatch for %s: ours %.2f (%s) vs Gamma %.2f "
                                "(%+.1f bps) - using Gamma", w.slug, w.price_to_beat, w.ptb_source,
                                w.gamma_ptb, w.ptb_check_bps)
            if w.ptb_source != "gamma":
                w.price_to_beat, w.ptb_source = w.gamma_ptb, "gamma"
                self.db.upsert_window(w)
            return
        # preference: Gamma > our Chainlink capture > previous window's official final > synthetic
        if w.ptb_source in ("chainlink", "prev_final"):
            return
        r = self.feeds.reference_twap_at(w.start) if now >= w.start + 1 else None
        if r is not None and not r[1]:
            w.price_to_beat, w.ptb_source = r[0], "chainlink"
            self.db.upsert_window(w)
            return
        prev = self.discovery.get(w.duration, w.start - DURATION_SECONDS[w.duration])
        if prev.final_price is not None:
            w.price_to_beat, w.ptb_source = prev.final_price, "prev_final"
            self.db.upsert_window(w)
            return
        if r is not None and w.price_to_beat is None:
            w.price_to_beat, w.ptb_source = r[0], "synthetic"

    # ------------------------------------------------------------ evaluation
    def books_for(self, w: Window) -> dict:
        return {"UP": self.clob.store.get(w.token_up), "DOWN": self.clob.store.get(w.token_down)}

    def evaluate(self, w: Window, now: float) -> WindowEval:
        ev = WindowEval(w, ts=now)
        gates = ev.gates
        q = self.feeds.quality.state
        if not w.found:
            gates.append("market not found on Polymarket yet")
        if w.rules is not None and not w.rules.ok:
            gates.append("RULES MISMATCH: " + "; ".join(w.rules.mismatches))
            if not getattr(w, "_rules_alerted", False):
                w._rules_alerted = True  # type: ignore[attr-defined]
                self.alert("rules", f"{w.duration}: RULES MISMATCH - no trading",
                           "; ".join(w.rules.mismatches), "warn")
        if q.degraded:
            gates.append("DEGRADED DATA: " + "; ".join(q.reasons))
        self.ensure_price_to_beat(w, now)
        if w.price_to_beat is None:
            gates.append("price to beat unknown")
        elif w.ptb_source == "synthetic":
            gates.append("price to beat estimated without Chainlink")
        if w.found and not w.accepting_orders:
            gates.append("market not accepting orders")
        left = w.end - now
        max_left = float(self.sc.get(f"max_seconds_left_{w.duration}", DURATION_SECONDS[w.duration]))
        if left < float(self.sc.get("min_seconds_left", 10)):
            gates.append(f"only {left:.0f}s left")
        elif left > max_left:
            gates.append(f"too early ({left:.0f}s left)")
        bl = blackout_overlaps(self.calendar.events if self.calendar.enabled else [], now, w.end,
                               self.calendar.before, self.calendar.after)
        if bl:
            gates.append("calendar blackout: " + ", ".join(
                f"{e.title} @ {time.strftime('%H:%M', time.localtime(e.ts))}" for e in bl[:3]))
        shock = self.news.shock(now) if self.news.enabled else None
        if shock:
            gates.append(f"news shock ({shock.label} {shock.confidence:.0%}): {shock.title[:80]}")

        books = self.books_for(w)
        for side, b in books.items():
            if b is None or now - b["updated"] > BOOK_STALE_S:
                gates.append(f"{side} order book stale")
                tok = w.token_up if side == "UP" else w.token_down
                if tok and now - self._rest_refresh.get(tok, 0) > 5:
                    self._rest_refresh[tok] = now
                    asyncio.create_task(self._safe_rest(tok))

        ref = self.feeds.reference(now)
        bars_src = self.feeds.primary_bars()
        sec = int(now) - 1
        bars = bars_src.window(sec, 960) if bars_src is not None else None
        real_secs = len(bars_src) if bars_src is not None else 0
        ref_px = ref.spot if ref.spot is not None else ref.twap
        if bars is None or ref_px is None or w.price_to_beat is None:
            if bars is None or real_secs < 30:
                gates.append("waiting for price data")
            self.evals[w.duration] = ev
            ev.decision = Decision("NO TRADE", None, {}, gates)
            return ev
        if now - self._started < WARMUP_SECONDS:
            gates.append(f"warming up ({WARMUP_SECONDS - (now - self._started):.0f}s)")

        L = w.lookback_s
        locked = self.feeds.reference_locked_mean(w.end - L, sec) if left <= L else None
        dfeat = self.derivs.features()
        fs = compute_features(
            bars=bars, ref_spot=ref_px, price_to_beat=w.price_to_beat, seconds_left=left,
            lookback_s=L, locked_mean=locked,
            vol_halflife_s=float(self.cfg.get("model", {}).get("vol_halflife_s", 900)),
            min_vol=float(self.cfg.get("model", {}).get("min_vol_per_s", 2e-5)),
            vol_step_s=int(self.cfg.get("model", {}).get("vol_step_s", 60)),
            book_imb=dfeat.get("book_imb", 0.0), derivs=dfeat,
            news_score=self.news.score(now) if self.news.enabled else 0.0)
        p_up, parts = self.models[w.duration].predict(fs)
        ev.p_up, ev.p_prior, ev.features = p_up, fs.p_prior, fs
        ev.decision = decide(p_up, books, buffer=float(self.sc.get("buffer", 0.04)),
                             stake_usd=float(self.sc.get("stake_usd", 50)),
                             max_spread=float(self.sc.get("max_spread", 0.04)),
                             fee_schedule=w.fee_schedule, gates=gates)
        ev.drivers = top_drivers(parts, fs, ev.decision.best_side or ("UP" if p_up >= 0.5 else "DOWN"))
        self.evals[w.duration] = ev
        return ev

    async def _safe_rest(self, tok: str) -> None:
        try:
            await self.clob.refresh_rest(tok)
        except Exception as e:  # noqa: BLE001
            log.debug("REST book refresh failed: %s", e)

    def _record(self, ev: WindowEval, now: float) -> None:
        w = ev.window
        ref = self.feeds.reference(now)
        books = self.books_for(w)
        spot = self.feeds.spot_prices()
        up, dn = books["UP"] or {}, books["DOWN"] or {}
        best = lambda b, k: b[k][0][0] if b and b.get(k) else None  # noqa: E731
        self.db.execute("INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            now, w.slug, w.end - now, ref.spot, ref.twap, ref.source,
            spot.get(self.feeds.active_spot) if self.feeds.active_spot else None,
            self.feeds.active_spot, best(up, "bids"), best(up, "asks"), best(dn, "bids"),
            best(dn, "asks"), ev.p_up, int(self.feeds.quality.state.degraded)))
        if ev.p_up is not None and now - self._last_pred.get(w.slug, 0) >= 15:
            self._last_pred[w.slug] = now
            self.db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,NULL)", (
                w.slug, now, w.end - now, ev.p_up, ev.p_prior, _mid(books["UP"]),
                int(self.feeds.quality.state.degraded or bool(
                    [g for g in ev.gates if g.startswith(("RULES", "price to beat", "warming"))]))))

    def _maybe_signal(self, ev: WindowEval, now: float) -> None:
        w, d = ev.window, ev.decision
        if d is None:
            return
        prev = self.last_action.get(w.slug, "NO TRADE")
        self.last_action[w.slug] = d.action
        if d.action == "NO TRADE" or d.action == prev:
            return
        if self.entries.get(w.slug, 0) >= int(self.sc.get("max_entries_per_window", 1)):
            return
        self.entries[w.slug] = self.entries.get(w.slug, 0) + 1
        q = d.quotes[d.action]
        p_side = ev.p_up if d.action == "UP" else 1 - ev.p_up
        sid = self.db.insert("signals", {
            "ts": now, "slug": w.slug, "duration": w.duration, "action": d.action,
            "p_up": ev.p_up, "p_side": p_side, "mkt_price": q.avg_fill, "edge": d.edge,
            "seconds_left": w.end - now, "drivers": json.dumps(ev.drivers),
            "blocked": json.dumps(d.blocked_by),
            "features": json.dumps(ev.features.values if ev.features else {})})
        book = self.books_for(w)[d.action] or {}
        self.paper.open(signal_id=sid, slug=w.slug, duration=w.duration, side=d.action,
                        asks=book.get("asks", []), p_model=p_side, edge=d.edge,
                        fee_schedule=w.fee_schedule)
        why = "; ".join(x["text"] for x in ev.drivers)
        self.alert("signal", f"{w.duration}: BUY {d.action} @ ≤{q.best_ask:.2f}",
                   f"model {p_side:.0%} vs ask {q.best_ask:.2f}, edge {d.edge:+.1%}, "
                   f"{w.end - now:.0f}s left. {why}\nhttps://polymarket.com/event/{w.slug}", "signal")
        log.info("SIGNAL %s %s edge=%.3f p=%.3f", w.slug, d.action, d.edge, p_side)

    async def _signal_loop(self) -> None:
        interval = float(self.sc.get("eval_interval_s", 1))
        while True:
            now = time.time()
            try:
                assets = set()
                for dur in self.durations:
                    for w in (self.discovery.current(dur, now), self.discovery.next(dur, now)):
                        assets.update((w.token_up, w.token_down))
                self.clob.set_assets(assets)
                q = self.feeds.quality.state
                if self._was_degraded is not None and q.degraded != self._was_degraded:
                    if q.degraded:
                        self.alert("degraded", "DEGRADED DATA - signals off", "; ".join(q.reasons), "warn")
                    else:
                        self.alert("degraded", "Data quality OK - signals back on", "", "info")
                self._was_degraded = q.degraded
                for dur in self.durations:
                    w = self.discovery.current(dur, now)
                    ev = self.evaluate(w, now)
                    self._record(ev, now)
                    self._maybe_signal(ev, now)
            except Exception:
                log.exception("signal loop error")
            want = max(0.05, interval - (time.time() - now))
            t_sleep = time.time()
            await asyncio.sleep(want)
            lag = (time.time() - t_sleep - want) * 1000
            self.loop_lag_ms = 0.9 * self.loop_lag_ms + 0.1 * lag
            if lag > 500:
                log.warning("event loop lagging %.0f ms - feeds may be dropped", lag)

    # ------------------------------------------------------------ exits
    def _advise(self, slug: str, side: str, entry: float, shares: float, cost: float,
                now: float) -> dict:
        w = self.discovery.windows.get(slug)
        if w is None:
            return {"action": "HOLD", "reason": "window unknown"}
        if w.outcome:
            return {"action": "SETTLED", "reason": f"resolved {w.outcome}",
                    "won": w.outcome == side}
        if now >= w.end:
            return {"action": "HOLD", "reason": "window closed - awaiting resolution"}
        ev = self.evals.get(w.duration)
        if ev is None or ev.window.slug != slug or ev.p_up is None:
            return {"action": "HOLD", "reason": "no model estimate for this window yet"}
        fair = ev.p_up if side == "UP" else 1 - ev.p_up
        book = self.books_for(w)[side] or {}
        q = self.feeds.quality.state
        adv = evaluate_exit(entry_price=entry, shares=shares, cost_basis=cost, fair=fair,
                            bids=book.get("bids", []), fee_schedule=w.fee_schedule,
                            hold_buffer=float(self.xc.get("hold_buffer", 0.01)),
                            stop_threshold=float(self.xc.get("stop_threshold", 0.15)),
                            take_profit=float(self.xc.get("take_profit", 0.20)),
                            lock_remaining_ev=float(self.xc.get("lock_remaining_ev", 0.03)),
                            blocked_reason="; ".join(q.reasons) if q.degraded else None)
        return adv.as_dict()

    async def _exit_loop(self) -> None:
        interval = float(self.xc.get("eval_interval_s", 2))
        while True:
            now = time.time()
            try:
                for p in self.db.query("SELECT * FROM positions WHERE closed_ts IS NULL"):
                    key = f"pos:{p['id']}"
                    adv = self._advise(p["slug"], p["side"], p["entry_price"], p["shares"],
                                       p["entry_price"] * p["shares"], now)
                    prev = self.position_advice.get(key, {}).get("action")
                    if adv["action"] in ("SELL", "HOLD") and prev and prev != adv["action"]:
                        px = adv.get("exit_price")
                        self.alert("exit", f"Position #{p['id']} {p['duration']} {p['side']}: {adv['action']}"
                                   + (f" @ ~{px:.2f}" if adv["action"] == "SELL" and px else ""),
                                   adv["reason"], "signal" if adv["action"] == "SELL" else "info")
                    self.position_advice[key] = adv
                for t in self.paper.open_trades():
                    adv = self._advise(t["slug"], t["side"], t["entry_price"], t["shares"],
                                       t["cost"] + t["entry_fee"], now)
                    self.position_advice[f"paper:{t['id']}"] = adv
                    if adv.get("action") == "SELL" and not adv["reason"].startswith("data unreliable"):
                        w = self.discovery.windows.get(t["slug"])
                        book = self.books_for(w)[t["side"]] if w else None
                        if book and book["bids"]:
                            self.paper.exit(t, book["bids"], adv["reason"], w.fee_schedule)
            except Exception:
                log.exception("exit loop error")
            await asyncio.sleep(max(0.1, interval - (time.time() - now)))

    # ------------------------------------------------------------ positions API
    def add_position(self, duration: str, side: str, entry_price: float, shares: float,
                     slug: str | None = None) -> int:
        w = self.discovery.windows.get(slug) if slug else self.discovery.current(duration)
        if w is None:
            raise ValueError("unknown window")
        return self.db.insert("positions", {"slug": w.slug, "duration": w.duration,
                                            "side": side.upper(), "entry_price": entry_price,
                                            "shares": shares, "opened_ts": time.time()})

    def close_position(self, pid: int) -> None:
        self.db.execute("UPDATE positions SET closed_ts=? WHERE id=?", (time.time(), pid))
        self.position_advice.pop(f"pos:{pid}", None)

    # ------------------------------------------------------------ snapshot
    def stats(self) -> dict:
        ts, s = self._stats_cache
        if time.time() - ts > 10:
            s = paper_stats(self.db)
            self._stats_cache = (time.time(), s)
        return s

    def _modules_snapshot(self, now: float) -> dict:
        out = {}
        for name, t in getattr(self, "traders", {}).items():
            snap = t.snapshot(now)
            snap["module_state"] = t.module.snapshot()
            snap["by_league"] = self._breakdown(t, "league")
            snap["by_bet_type"] = self._breakdown(t, "bet_type")
            out[name] = snap
        if getattr(self, "global_risk", None) is not None:
            out["_global"] = self.global_risk.summary(now)
        return out

    @staticmethod
    def _breakdown(t, col: str) -> dict:
        from .trading.store import bot_stats
        rows = t.store.closed_positions()
        groups: dict = {}
        for r in rows:
            groups.setdefault(r.get(col) or "-", []).append(r)
        out = {}
        for k, rs in groups.items():
            st = bot_stats(rs)
            clvs = [r["clv"] for r in rs if r.get("clv") is not None]
            st["avg_clv"] = sum(clvs) / len(clvs) if clvs else None
            st["clv_n"] = len(clvs)
            out[k] = st
        return out

    def snapshot(self) -> dict:
        now = time.time()
        ref = self.feeds.reference(now)
        q = self.feeds.quality.state
        windows = {}
        for dur in self.durations:
            w = self.discovery.current(dur, now)
            ev = self.evals.get(dur)
            books = self.books_for(w)
            d = ev.decision if ev and ev.window.slug == w.slug else None
            windows[dur] = {
                "window": w.as_dict(now),
                "p_up": ev.p_up if ev and ev.window.slug == w.slug else None,
                "p_prior": ev.p_prior if ev and ev.window.slug == w.slug else None,
                "d_sigma": ev.features.d_sigma if ev and ev.features and ev.window.slug == w.slug else None,
                "sigma": ev.features.sigma if ev and ev.features and ev.window.slug == w.slug else None,
                "market": {"up_mid": _mid(books["UP"]), "down_mid": _mid(books["DOWN"]),
                           "up_bid": books["UP"]["bids"][0][0] if books["UP"] and books["UP"]["bids"] else None,
                           "up_ask": books["UP"]["asks"][0][0] if books["UP"] and books["UP"]["asks"] else None,
                           "down_bid": books["DOWN"]["bids"][0][0] if books["DOWN"] and books["DOWN"]["bids"] else None,
                           "down_ask": books["DOWN"]["asks"][0][0] if books["DOWN"] and books["DOWN"]["asks"] else None},
                "decision": d.as_dict() if d else {"action": "NO TRADE", "blocked_by": ["starting"]},
                "drivers": ev.drivers if ev and ev.window.slug == w.slug else [],
                "features": ev.features.values if ev and ev.features and ev.window.slug == w.slug else {},
                "model": self.models[dur].meta,
            }
        positions = []
        for p in self.db.query("SELECT * FROM positions WHERE closed_ts IS NULL ORDER BY id DESC"):
            positions.append({**p, "advice": self.position_advice.get(f"pos:{p['id']}", {})})
        paper_open = []
        for t in self.db.query("SELECT * FROM paper_trades WHERE status IN ('open','exited') "
                               "ORDER BY id DESC LIMIT 10"):
            paper_open.append({**t, "advice": self.position_advice.get(f"paper:{t['id']}", {})})
        spot = self.feeds.spot_prices()
        return {
            "ts": now,
            "reference": {"spot": ref.spot, "twap": ref.twap, "source": ref.source,
                          "synthetic": ref.synthetic},
            "spot": spot, "active_spot": self.feeds.active_spot,
            "quality": q.as_dict(), "feeds": self.feeds.health(),
            "loop_lag_ms": round(self.loop_lag_ms, 1),
            "clob": {"connected": self.clob.connected, "reconnects": self.clob.reconnects, "last_msg_age": round(now - self.clob.last_msg, 1)
                     if self.clob.last_msg else None, "error": self.clob.last_error},
            "discovery_error": self.discovery.last_error,
            "windows": windows, "positions": positions, "paper_open": paper_open,
            "stats": self.stats(),
            "news": [h.as_dict() for h in self.news.recent(now)[:12]],
            "news_score": self.news.score(now), "news_errors": self.news.errors,
            "calendar": {"upcoming": [e.as_dict() for e in self.calendar.upcoming(now)],
                         "active": [e.as_dict() for e in self.calendar.blackouts(now)],
                         "error": self.calendar.error},
            "derivs": self.derivs.snapshot(),
            "notifications": self.notifier.status(),
            "alerts": list(self.alerts)[-20:],
            "bot": self.trader.snapshot(now) if self.trader else {"mode": "off"},
            "modules": self._modules_snapshot(now),
            "venue": self.venue,
            "live": {"phase": self.live_phase,
                     "reconcile": self.reconciler.snapshot() if self.reconciler else None,
                     "trades_today": (self.global_risk.trades_today_fn() if self.global_risk is not None
                                      and self.global_risk.trades_today_fn else None)},
        }
