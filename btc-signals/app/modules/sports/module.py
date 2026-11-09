"""Sports strategy module (Polymarket US): free-tier sportsbook consensus vs Polymarket price.

Fair value = average of de-vigged DraftKings / FanDuel / BetMGM probabilities for the exact
same market and line (TheRundown, SharpAPI). Research only blocks or annotates; it never
moves the number by itself (see research.py). A Pinnacle check (The Odds API, 1 credit) is
spent only on games that already show an edge, within `pinnacle_check.window_min` of start,
and is required for any entry in the last `no_entry_before_start_min` minutes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

import httpx

from ...fees import fee_per_share
from ...ingest.polymarket_ws import BookStore
from ...ingest.us_markets import us_book_to_store
from ...trading.rules import ExitDecision
from ..base import MarketInfo, Reason, Signal, StrategyModule
from .markets import SportsGame, SportsMarket, liquidity_ok, parse_event
from .odds import (Fair, OddsEvent, build_sources, canonicalize, consensus, devig, map_teams,
                   match_event)
from .research import GameResearch, Research

log = logging.getLogger("sports")

GATEWAY = "https://gateway.polymarket.us"
LINES_SCHEMA = """
CREATE TABLE IF NOT EXISTS sports_lines (
  ts REAL, league TEXT, event_slug TEXT, market_slug TEXT, bet_type TEXT, line REAL,
  fair_long REAL, n_books INTEGER, newest REAL, pm_mid REAL, sources TEXT
);
CREATE INDEX IF NOT EXISTS sports_lines_mkt ON sports_lines(market_slug, ts);
CREATE TABLE IF NOT EXISTS sports_closing (
  market_slug TEXT PRIMARY KEY, league TEXT, event_slug TEXT, bet_type TEXT, line REAL,
  starts_at REAL, closing_fair_long REAL, closing_ts REAL, n_books INTEGER, pm_mid_at_close REAL,
  settled_long REAL
);
"""


def confidence_of(f: Fair, now: float, min_books: int) -> float:
    base = {1: 0.5, 2: 0.75}.get(f.n_books, 0.9) if f.n_books >= min_books else 0.3
    base -= min(0.3, f.spread_between_books * 4)          # books disagree -> less sure
    age_min = (now - f.newest) / 60
    if age_min > 15:
        base *= 0.7
    return max(0.0, min(1.0, base))


class SportsModule(StrategyModule):
    name = "sports"

    def __init__(self, engine, cfg: dict, client: Optional[httpx.AsyncClient] = None):
        self.engine = engine
        self.cfg = cfg
        self.sc = cfg.get("sports", {}) or {}
        self.client = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "btc-signals/1.0"})
        self.db = engine.db
        self.db.conn.executescript(LINES_SCHEMA)
        self.db.conn.commit()
        self.store = None                                   # TradeStore, attached by the engine
        self.sources: dict = {}                             # built in attach() so quota ledgers load from the DB
        self.research = Research(self.sc.get("research", {}), self.client)
        self.books = BookStore()
        self._last_shares: dict = {}
        self.games: dict[str, SportsGame] = {}              # event_slug -> game
        self.market_index: dict[str, SportsMarket] = {}     # market slug -> market (kept until settled)
        self.odds_events: dict[str, list[OddsEvent]] = {}   # "source:league" -> events
        self.odds_fetched: dict[str, float] = {}
        self.fairs: dict[str, tuple] = {}                   # market slug -> (Fair, fair_long, ts)
        self.board: list[dict] = []
        self.current_signals: list[Signal] = []
        self.research_by_game: dict[str, GameResearch] = {}
        self.pinnacle: dict = {}                            # (event_slug, bet_type) -> (ts, ok, detail)
        self.settled: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self._gw_sem = asyncio.Semaphore(int(self.sc.get("gateway_concurrency", 4)))
        self._tasks: list[asyncio.Task] = []
        self._state: dict = {}
        self._last_settle_poll: dict = {}

    def attach(self, store) -> None:
        """Give the module its trade store; quota ledgers (Budget) persist through it."""
        self.store = store
        self.sources = build_sources((self.sc.get("odds", {}) or {}).get("sources", {}), self, self.client)

    # state for Budget persistence (get_state/set_state protocol)
    def get_state(self, key, default=None):
        if self.store is not None:
            return self.store.get_state(key, default)
        return self._state.get(key, default)

    def set_state(self, key, value):
        if self.store is not None:
            self.store.set_state(key, value)
        self._state[key] = value

    # ------------------------------------------------------------------ config helpers
    def leagues(self) -> list[str]:
        return [k for k, v in (self.sc.get("leagues") or {}).items() if (v or {}).get("enabled")]

    def league_conf(self, league: str) -> dict:
        return (self.sc.get("leagues") or {}).get(league) or {}

    def buffer(self, league: str) -> float:
        return float(self.league_conf(league).get("buffer", self.sc.get("edge_buffer", 0.07)))

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.sources:
            self.sources = build_sources((self.sc.get("odds", {}) or {}).get("sources", {}), self, self.client)
        for name, coro in (("sports-discovery", self._discovery_loop()), ("sports-odds", self._odds_loop()),
                           ("sports-books", self._books_loop()), ("sports-eval", self._eval_loop())):
            self._tasks.append(asyncio.create_task(self._guard(name, coro), name=name))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _guard(self, name, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sports task %s crashed", name)

    async def _gw(self, path: str, params: Optional[dict] = None):
        async with self._gw_sem:
            r = await self.client.get(GATEWAY + path, params=params)
            await asyncio.sleep(0.15)                       # keep well under 25 req/s shared limit
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ discovery
    async def refresh_games(self) -> None:
        now = time.time()
        horizon = now + float(self.sc.get("lookahead_hours", 36)) * 3600
        for league in self.leagues():
            try:
                d = await self._gw(f"/v2/leagues/{league}/events", {"limit": 100})
                for ev in (d or {}).get("events") or []:
                    from .markets import _ts
                    st = _ts(ev.get("startDate") or ev.get("startTime"))
                    if st is None or st < now - 6 * 3600 or st > horizon:
                        continue
                    slug = ev.get("slug")
                    g = self.games.get(slug)
                    stale = g is None or now - g.fetched > float(self.sc.get("event_refresh_s", 600))
                    if stale and st > now:
                        full = await self._gw(f"/v1/events/slug/{slug}")
                        if full and full.get("event"):
                            g = parse_event(full["event"], league)
                            self.games[slug] = g
                            self.market_index.update(g.markets)
                self.errors.pop(f"discovery:{league}", None)
            except Exception as e:  # noqa: BLE001
                self.errors[f"discovery:{league}"] = f"{type(e).__name__}: {e}"[:160]
        for slug in [s for s, g in self.games.items() if g.starts_at and g.starts_at < now - 8 * 3600]:
            del self.games[slug]

    async def _discovery_loop(self) -> None:
        while True:
            await self.refresh_games()
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ odds
    def _refresh_due(self, league: str, now: float) -> float:
        sch = (self.sc.get("odds", {}) or {}).get("schedule", {}) or {}
        starts = [g.starts_at for g in self.games.values() if g.league == league and g.starts_at and g.starts_at > now]
        if not starts:
            return float(sch.get("idle_refresh_s", 4 * 3600))
        soonest = min(starts) - now
        if soonest <= float(sch.get("closing_capture_min", 10)) * 60 + 300:
            return float(sch.get("closing_refresh_s", 300))
        if soonest <= float(sch.get("near_window_h", 2)) * 3600:
            return float(sch.get("near_refresh_s", 1800))
        return float(sch.get("far_refresh_s", 3 * 3600))

    async def refresh_odds(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        for league in self.leagues():
            if not any(g.league == league for g in self.games.values()):
                continue
            for name, src in self.sources.items():
                if name == "theoddsapi" or not src.available:
                    continue
                key = f"{name}:{league}"
                every = self._refresh_due(league, now)
                if name == "sharpapi":
                    every = min(every, float((self.sc.get("odds", {}) or {}).get("sharpapi_refresh_s", 120)))
                if now - self.odds_fetched.get(key, 0) < every:
                    continue
                try:
                    self.odds_events[key] = await src.fetch_league(league)
                    self.odds_fetched[key] = now
                except Exception as e:  # noqa: BLE001
                    src.last_error = f"{type(e).__name__}: {e}"[:160]
                    self.odds_fetched[key] = now - every + 300      # retry in 5 min

    async def _odds_loop(self) -> None:
        while True:
            await self.refresh_odds()
            await asyncio.sleep(30)

    def quotes_for(self, g: SportsGame) -> list:
        quotes = []
        for key, evs in self.odds_events.items():
            if not key.endswith(f":{g.league}"):
                continue
            e = match_event(evs, g.team_names, g.starts_at)
            if e is None:
                continue
            mapping = map_teams([e.home, e.away], g.team_names)
            if mapping:
                quotes += canonicalize(e.quotes, mapping)
        return quotes

    def fair_for(self, m: SportsMarket, quotes: list, now: float) -> Optional[Fair]:
        oc = self.sc.get("odds", {}) or {}
        kw = dict(method=oc.get("method", "power"), max_age_s=float(oc.get("max_quote_age_s", 2700)), now=now,
                  books=oc.get("books") or None)
        if m.bet_type == "moneyline":
            return consensus(quotes, "moneyline", m.long_team, None, other_team=m.short_team, **kw)
        if m.bet_type == "spread":
            return consensus(quotes, "spread", m.long_team, m.line, other_team=m.short_team, **kw)
        return consensus(quotes, "total", "over", m.line, **kw)

    # ------------------------------------------------------------------ books
    def watched_slugs(self) -> set:
        s = set(self.fairs)
        s |= {m.slug for g in self.games.values() for m in g.markets.values()
              if m.bet_type == "moneyline" and g.starts_at and g.starts_at > time.time()}
        if self.store is not None:
            s |= {p["slug"] for p in self.store.active_positions()}
        return s

    async def poll_book(self, slug: str) -> None:
        d = await self._gw(f"/v1/markets/{slug}/book")
        if d:
            us_book_to_store(self.books, slug, d, time.time(), self._last_shares)

    async def _books_loop(self) -> None:
        last: dict = {}
        while True:
            now = time.time()
            held = {p["slug"] for p in self.store.active_positions()} if self.store is not None else set()
            every = float(self.sc.get("book_refresh_s", 60))
            due = [s for s in self.watched_slugs()
                   if now - last.get(s, 0) >= (5.0 if s in held else every)]
            for s in due:
                try:
                    await self.poll_book(s)
                except Exception as e:  # noqa: BLE001
                    self.errors["books"] = f"{type(e).__name__}: {e}"[:160]
                last[s] = now
            await asyncio.sleep(2)

    def book(self, token: str) -> Optional[dict]:
        return self.books.get(token)

    # ------------------------------------------------------------------ evaluation
    async def evaluate(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        board, sigs = [], []
        allowed = set(self.sc.get("bet_types", ["moneyline", "spread", "total"]))
        liq = self.sc.get("liquidity", {}) or {}
        pre_min = float(self.sc.get("no_entry_before_start_min", 15))
        live = bool(self.sc.get("live_betting", False))
        for g in sorted(self.games.values(), key=lambda x: x.starts_at or 0):
            if g.starts_at is None or (g.started(now) and not live):
                self._capture_closing(g, now)
                continue
            quotes = self.quotes_for(g)
            res = self.research_by_game.get(g.event_slug)
            if res is None or now - getattr(res, "_ts", 0) > 300:
                try:
                    res = await self.research.game(g.league, g.team_names, g.starts_at)
                    res._ts = now  # type: ignore[attr-defined]
                    self.research_by_game[g.event_slug] = res
                except Exception as e:  # noqa: BLE001
                    self.errors["research"] = f"{type(e).__name__}: {e}"[:160]
                    res = GameResearch()
            for m in g.markets.values():
                if m.bet_type not in allowed:
                    continue
                f = self.fair_for(m, quotes, now) if quotes else None
                if f is None:
                    if m.bet_type == "moneyline":             # still show the game and its price
                        board.append(self._price_only_row(g, m, quotes))
                    continue                                  # line not quoted by books: exact-line rule
                self.fairs[m.slug] = (f, f.prob, now)
                self._record_line(m, f, now)
                row = self._board_row(g, m, f, res, now, liq, pre_min)
                board.append(row)
                if row.get("signal"):
                    sigs.append(row.pop("signal"))
                else:
                    row.pop("signal", None)
        self.board = board
        self.current_signals = sigs

    def _board_row(self, g: SportsGame, m: SportsMarket, f: Fair, res: GameResearch, now: float,
                   liq: dict, pre_min: float) -> dict:
        oc = self.sc.get("odds", {}) or {}
        min_books = int(oc.get("min_books", 2))
        buf = self.buffer(g.league)
        conf = confidence_of(f, now, min_books)
        reasons: list[Reason] = [Reason(
            f"Consensus {f.n_books} book(s) ({', '.join(f'{k} {v:.1%}' for k, v in f.books.items())}) "
            f"fair {m.long_label if m.bet_type != 'total' else 'Over'} {f.prob:.1%}",
            "+".join(f.sources), f.prob, f.newest)]
        move = self._line_move(m, f, now)
        if move:
            reasons.append(move)
        reasons += res.reasons[:3]
        best = None
        out = {"league": g.league, "game": g.title, "event_slug": g.event_slug, "starts_at": g.starts_at,
               "market": m.slug, "bet_type": m.bet_type, "line": m.line, "fair_long": round(f.prob, 4),
               "n_books": f.n_books, "confidence": round(conf, 2), "flags": list(res.flags), "signal": None}
        blocks: list[str] = []
        if not m.rules_ok:
            blocks.append("rules: " + "; ".join(m.rules_issues))
        if f.n_books < min_books:
            blocks.append(f"only {f.n_books} book(s) quote this line")
        if res.newest_news_ts and res.newest_news_ts > f.newest:
            blocks.append("fresh injury/news newer than the odds: waiting for books to update")
        for token, label in ((m.token_long, m.side_label(m.token_long)), (m.token_short, m.side_label(m.token_short))):
            fair_side = f.prob if token.endswith(":long") else 1 - f.prob
            bk = self.books.get(token)
            ok, why = liquidity_ok(bk, float(liq.get("max_spread", 0.03)), float(liq.get("min_depth", 200)))
            if not bk or not bk.get("asks"):
                continue
            ask = bk["asks"][0][0]
            edge = fair_side - ask - fee_per_share(ask, m.fee_schedule) - buf
            side = {"token": token, "label": label, "fair": round(fair_side, 4), "ask": ask,
                    "bid": bk["bids"][0][0] if bk.get("bids") else None, "edge": round(edge, 4),
                    "liquidity_ok": ok, "liquidity": why}
            if best is None or edge > best["edge"]:
                best = side
        out["best"] = best
        if best is None:
            blocks.append("no Polymarket book yet")
        else:
            out["pm_mid"] = ((best["bid"] or best["ask"]) + best["ask"]) / 2
            if not best["liquidity_ok"]:
                blocks.append(f"illiquid: {best['liquidity']}")
            if best["edge"] <= 0:
                blocks.append(f"edge {best['edge']:+.1%} after fees and {buf:.0%} buffer")
        mins_left = ((g.starts_at or now) - now) / 60
        if best and best["edge"] > 0 and not blocks:
            chk = self.pinnacle.get((g.event_slug, m.bet_type))
            window = float((oc.get("pinnacle_check") or {}).get("window_min", 60))
            if mins_left <= window and chk is None:
                asyncio.ensure_future(self._pinnacle(g, m, best, f))
            if chk is not None:
                reasons.insert(1, Reason(chk[2], "The Odds API (Pinnacle)", 0.0, chk[0]))
                if chk[1] is False:
                    blocks.append("Pinnacle disagrees with the consensus")
            if mins_left <= pre_min and (chk is None or chk[1] is not True):
                blocks.append(f"inside {pre_min:.0f} min of start without a Pinnacle confirmation")
        out["blocked_by"] = blocks
        out["reasons"] = [r.as_dict() for r in reasons[:4]]
        if best and not blocks:
            out["signal"] = Signal(module=self.name, market_id=m.slug, token=best["token"], side=best["label"],
                                   fair_prob=best["fair"], confidence=conf, edge=best["edge"],
                                   reasons=reasons[:3], group=f"{g.league}:{g.event_slug}", league=g.league,
                                   bet_type=m.bet_type, label=g.league.upper(), starts_at=g.starts_at,
                                   expires_at=g.starts_at,
                                   stake_usd=float(self.league_conf(g.league).get("stake_usd") or 0) or None)
        return out

    def _price_only_row(self, g: SportsGame, m: SportsMarket, quotes: list) -> dict:
        bk = self.books.get(m.token_long)
        bid = bk["bids"][0][0] if bk and bk.get("bids") else None
        ask = bk["asks"][0][0] if bk and bk.get("asks") else None
        why = ("no odds source configured (add a free TheRundown/SharpAPI key)"
               if not any(s.available for n, s in self.sources.items() if n != "theoddsapi")
               else "sportsbooks not matched/quoted yet" if not quotes else "books don't quote this market")
        return {"league": g.league, "game": g.title, "event_slug": g.event_slug, "starts_at": g.starts_at,
                "market": m.slug, "bet_type": m.bet_type, "line": m.line, "fair_long": None, "n_books": 0,
                "confidence": 0, "flags": list((self.research_by_game.get(g.event_slug) or GameResearch()).flags),
                "best": {"token": m.token_long, "label": m.long_label, "fair": None, "ask": ask, "bid": bid,
                         "edge": None, "liquidity_ok": None, "liquidity": ""},
                "pm_mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
                "blocked_by": [why],
                "reasons": [r.as_dict() for r in (self.research_by_game.get(g.event_slug) or GameResearch()).reasons[:3]]}

    async def _pinnacle(self, g: SportsGame, m: SportsMarket, best: dict, f: Fair) -> None:
        """Spend 1 credit to compare with Pinnacle for a game that already shows an edge."""
        key = (g.event_slug, m.bet_type)
        self.pinnacle[key] = (time.time(), None, "Pinnacle check pending")
        src = self.sources.get("theoddsapi")
        if src is None or not src.available:
            self.pinnacle[key] = (time.time(), None, "Pinnacle check unavailable (no The Odds API key)")
            return
        try:
            evs = await src.list_events(g.league)                 # free endpoint
            cands = [OddsEvent("theoddsapi", e["id"], g.league, e.get("home_team", ""), e.get("away_team", ""),
                               None) for e in evs]
            for c, e in zip(cands, evs):
                from .odds import _iso
                c.starts_at = _iso(e["commence_time"]) if e.get("commence_time") else None
            ev = match_event(cands, g.team_names, g.starts_at)
            if ev is None:
                self.pinnacle[key] = (time.time(), None, "Pinnacle: game not found")
                return
            full = await src.pinnacle_check(g.league, ev.event_id, m.bet_type)
            if full is None:
                self.pinnacle[key] = (time.time(), None, f"Pinnacle check skipped: {src.last_error}")
                return
            mapping = map_teams([full.home, full.away], g.team_names) or {}
            pf = self.fair_for(m, canonicalize(full.quotes, mapping), time.time())
            if pf is None:
                self.pinnacle[key] = (time.time(), None, "Pinnacle doesn't quote this exact line")
                return
            side_fair = pf.prob if best["token"].endswith(":long") else 1 - pf.prob
            dis = abs(side_fair - best["fair"])
            max_dis = float(((self.sc.get("odds", {}) or {}).get("pinnacle_check") or {}).get("max_disagree", 0.03))
            ok = dis <= max_dis
            self.pinnacle[key] = (time.time(), ok, f"Pinnacle fair {side_fair:.1%} vs consensus {best['fair']:.1%} "
                                                   f"({'agrees' if ok else 'DISAGREES'})")
        except Exception as e:  # noqa: BLE001
            self.pinnacle[key] = (time.time(), None, f"Pinnacle check failed: {type(e).__name__}")

    def _line_move(self, m: SportsMarket, f: Fair, now: float) -> Optional[Reason]:
        rows = self.db.query("SELECT ts, fair_long, pm_mid FROM sports_lines WHERE market_slug=? AND ts<=? "
                             "ORDER BY ts DESC LIMIT 1", (m.slug, now - 1800))
        if not rows or rows[0]["fair_long"] is None:
            return None
        delta = f.prob - rows[0]["fair_long"]
        if abs(delta) < 0.03:
            return None
        bk = self.books.get(m.token_long)
        mid_now = (bk["bids"][0][0] + bk["asks"][0][0]) / 2 if bk and bk.get("bids") and bk.get("asks") else None
        pm_delta = (mid_now - rows[0]["pm_mid"]) if (mid_now is not None and rows[0]["pm_mid"]) else None
        lag = "" if pm_delta is None else f"; Polymarket moved {pm_delta:+.1%}"
        return Reason(f"Books moved {m.long_label if m.bet_type != 'total' else 'Over'} {delta:+.1%} in 30+ min{lag}",
                      "line history", delta, now)

    def _record_line(self, m: SportsMarket, f: Fair, now: float) -> None:
        last = self.db.query("SELECT ts, fair_long FROM sports_lines WHERE market_slug=? ORDER BY ts DESC LIMIT 1",
                             (m.slug,))
        if last and abs((last[0]["fair_long"] or 0) - f.prob) < 1e-6 and now - last[0]["ts"] < 1800:
            return                                            # unchanged: don't bloat the table
        bk = self.books.get(m.token_long)
        mid = (bk["bids"][0][0] + bk["asks"][0][0]) / 2 if bk and bk.get("bids") and bk.get("asks") else None
        self.db.execute("INSERT INTO sports_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (now, m.league, m.event_slug, m.slug, m.bet_type, m.line, f.prob, f.n_books, f.newest,
                         mid, json.dumps(f.sources)))

    def _capture_closing(self, g: SportsGame, now: float) -> None:
        """At/after start: last consensus before start = closing line (our own CLV dataset)."""
        if g.starts_at is None or now < g.starts_at:
            return
        for m in g.markets.values():
            if self.db.query("SELECT 1 FROM sports_closing WHERE market_slug=?", (m.slug,)):
                continue
            row = self.db.query("SELECT * FROM sports_lines WHERE market_slug=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                                (m.slug, g.starts_at))
            if not row:
                continue
            r = row[0]
            self.db.execute("INSERT OR REPLACE INTO sports_closing (market_slug,league,event_slug,bet_type,line,"
                            "starts_at,closing_fair_long,closing_ts,n_books,pm_mid_at_close) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (m.slug, m.league, m.event_slug, m.bet_type, m.line, g.starts_at, r["fair_long"],
                             r["ts"], r["n_books"], r["pm_mid"]))
            if self.store is not None:
                for p in self.store.db.query("SELECT id, token, entry_price FROM bot_positions WHERE slug=? "
                                             "AND module='sports' AND status!='void'", (m.slug,)):
                    side_close = r["fair_long"] if p["token"].endswith(":long") else 1 - r["fair_long"]
                    self.store.update_position(p["id"], closing_fair=side_close,
                                               clv=side_close - (p["entry_price"] or 0))

    async def _eval_loop(self) -> None:
        while True:
            try:
                await self.evaluate()
            except Exception:
                log.exception("sports evaluation failed")
            await asyncio.sleep(float(self.sc.get("eval_interval_s", 15)))

    # ------------------------------------------------------------------ StrategyModule API
    def signals(self, now: float) -> list[Signal]:
        return [s for s in self.current_signals if s.starts_at is None or now < s.starts_at]

    def market(self, slug: str) -> Optional[MarketInfo]:
        m = self.market_index.get(slug)
        if m is None:
            return None
        live = bool(self.sc.get("live_betting", False))
        st = m.starts_at
        return MarketInfo(slug=slug, tick_size=m.tick_size, min_size=m.min_size, fee_schedule=m.fee_schedule,
                          condition_id=slug, label=m.league.upper(),
                          entry_deadline=None if (live or st is None) else st - 120,
                          hold_phase_from=None if (live or st is None) else st,
                          seconds_left=(st - time.time()) if st else None)

    def fair(self, p: dict) -> tuple[Optional[float], bool]:
        x = self.fairs.get(p["slug"])
        if x is None or time.time() - x[2] > 3600:
            return None, False
        return (x[1] if p["token"].endswith(":long") else 1 - x[1]), True

    def exit_decision(self, p: dict, val, now: float) -> ExitDecision:
        x = self.sc.get("exits", {}) or {}
        fair, ok = self.fair(p)
        m = self.market_index.get(p["slug"])
        mins_left = ((m.starts_at - now) / 60) if (m and m.starts_at) else 1e9
        if val.shares_sellable <= 0:
            return ExitDecision("HOLD", "no bids to sell into")
        if val.pnl_pct >= float(x.get("take_profit_pct", 0.10)):
            return ExitDecision("SELL", f"take profit: {val.pnl_pct:+.1%} after fees", "take_profit")
        if ok and fair is not None:
            if val.net_per_share >= fair - float(x.get("fair_buffer", 0.0)):
                return ExitDecision("SELL", f"price reached fair value ({val.net_per_share:.3f} >= {fair:.3f})",
                                    "edge_gone")
            entry_fair = p.get("p_model") or fair
            if entry_fair - fair >= float(x.get("stop_fair_drop", 0.05)):
                return ExitDecision("SELL", f"new information moved fair value against us "
                                            f"({entry_fair:.1%} -> {fair:.1%})", "stop_loss")
        if mins_left <= float(x.get("sell_before_start_min", 5)):
            edge_left = (fair - val.net_per_share) if (ok and fair is not None) else -1
            if x.get("hold_if_edge", True) and edge_left >= float(x.get("hold_min_edge", 0.02)) and val.complete:
                return ExitDecision("HOLD", f"holding to the result: edge {edge_left:+.3f}/share persists at start",
                                    "hold_to_resolution")
            return ExitDecision("SELL", "selling before start: edge no longer holds" if ok else
                                "selling before start: no fresh fair value", "time_exit")
        return ExitDecision("HOLD", f"P&L {val.pnl_pct:+.1%}; fair {fair if fair is not None else float('nan'):.3f}")

    async def refresh_settlement(self, slug: str) -> Optional[float]:
        m = await self._gw(f"/v1/market/slug/{slug}")
        mk = (m or {}).get("market") or {}
        if mk.get("status") != "MARKET_STATUS_RESOLVED":
            return None
        s = await self._gw(f"/v1/markets/{slug}/settlement")
        if s is None or s.get("settlement") is None:
            return None
        self.settled[slug] = float(s["settlement"])
        self.db.execute("UPDATE sports_closing SET settled_long=? WHERE market_slug=?", (self.settled[slug], slug))
        return self.settled[slug]

    def settlement_per_share(self, p: dict) -> Optional[float]:
        v = self.settled.get(p["slug"])
        if v is None:
            if time.time() - self._last_settle_poll.get(p["slug"], 0) > 120:
                self._last_settle_poll[p["slug"]] = time.time()
                asyncio.ensure_future(self.refresh_settlement(p["slug"]))
            return None
        return v if p["token"].endswith(":long") else 1.0 - v

    def outcome_label(self, p: dict) -> str:
        v = self.settled.get(p["slug"])
        return f"settled at {v:.2f} (long side)" if v is not None else "settled"

    def entry_blockers(self, sig: Signal, active: list[dict], now: float) -> list[str]:
        rc = self.sc.get("risk", {}) or {}
        stake = float(sig.stake_usd or (self.sc.get("entry", {}) or {}).get("stake_usd", 5))

        def at_risk(p):
            if p["status"] == "opening":
                return stake
            held = (p["shares"] or 0) - (p["exit_shares"] or 0)
            return ((p["cost"] or 0) + (p["entry_fee"] or 0)) * (held / p["shares"]) if p["shares"] else 0.0

        game = sum(at_risk(p) for p in active if p.get("group_key") == sig.group)
        league = sum(at_risk(p) for p in active if p.get("league") == sig.league)
        out = []
        if game + stake > float(rc.get("max_exposure_per_game_usd", 15)) + 1e-9:
            out.append(f"game exposure ${game:.2f} + ${stake:.2f} > ${rc.get('max_exposure_per_game_usd', 15)} "
                       "(all positions in a game count as one)")
        if league + stake > float(rc.get("max_exposure_per_league_usd", 40)) + 1e-9:
            out.append(f"{sig.league} exposure ${league:.2f} + ${stake:.2f} > ${rc.get('max_exposure_per_league_usd', 40)}")
        return out

    def snapshot(self) -> dict:
        return {"board": self.board[:200], "signals": [s.as_dict() for s in self.current_signals],
                "leagues": self.leagues(), "games": len(self.games),
                "sources": {k: v.summary() for k, v in self.sources.items()},
                "research_errors": self.research.errors, "errors": self.errors,
                "pinnacle_checks": {f"{k[0]}:{k[1]}": v[2] for k, v in self.pinnacle.items()}}
