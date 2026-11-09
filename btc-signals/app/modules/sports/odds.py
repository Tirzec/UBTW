"""Odds sources behind one interface, quota budgets, de-vig and consensus fair probabilities.

Free tiers only (limits verified in each provider's docs on 2026-11-09):
  TheRundown   20,000 data points/UTC day and 200,000/UTC month; 1 req/s; 5-min delay;
               DraftKings(19), FanDuel(23), BetMGM(22). Cost = 2/event + 1/price.
               Usage headers X-Datapoints / X-Datapoints-Remaining.
  SharpAPI     12 requests/min, no monthly cap; 60-s delay; DraftKings, FanDuel.
               Header X-RateLimit-Remaining.
  The Odds API 500 credits/month; events list free; per-event odds = markets x regions,
               bookmakers=pinnacle counts as one region -> 1 game x 1 market = 1 credit.
               Headers x-requests-remaining / x-requests-used.
Every source returns Quote rows; the consensus takes, per book, the NEWEST quote from any
source, de-vigs each book's two-way (or three-way) market and averages the books.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from .markets import normalize_team

log = logging.getLogger("sports.odds")


# ---------------------------------------------------------------------------- data model
@dataclass
class Quote:
    source: str                  # therundown | sharpapi | theoddsapi
    book: str                    # draftkings | fanduel | betmgm | pinnacle
    market: str                  # moneyline | spread | total
    team: str                    # full team name for moneyline/spread; "over"/"under" for totals; "draw"
    line: Optional[float]        # spread handicap for `team`, or total points
    american: float
    updated: float               # source timestamp (unix)
    fetched: float = 0.0

    @property
    def implied(self) -> float:
        return american_to_prob(self.american)


@dataclass
class OddsEvent:
    source: str
    event_id: str
    league: str
    home: str
    away: str
    starts_at: Optional[float]
    quotes: list = field(default_factory=list)


def american_to_prob(a: float) -> float:
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def devig(probs: list[float], method: str = "power") -> list[float]:
    """Remove the bookmaker margin. power: find k with sum(p_i^k) = 1 (handles the
    favourite-longshot bias better than multiplicative); multiplicative: p_i / sum."""
    s = sum(probs)
    if s <= 0:
        return probs
    if method == "multiplicative" or len(probs) < 2:
        return [p / s for p in probs]
    lo, hi = 0.5, 3.0
    for _ in range(60):
        k = (lo + hi) / 2
        t = sum(p ** k for p in probs)
        lo, hi = (k, hi) if t > 1 else (lo, k)
    k = (lo + hi) / 2
    out = [p ** k for p in probs]
    z = sum(out)
    return [p / z for p in out]


@dataclass
class Fair:
    prob: float                  # fair probability of the requested side
    n_books: int
    books: dict                  # book -> fair prob for the side
    spread_between_books: float  # max - min across books
    newest: float                # newest quote timestamp used
    oldest: float
    sources: list

    def as_dict(self) -> dict:
        return {"prob": round(self.prob, 4), "n_books": self.n_books,
                "books": {k: round(v, 4) for k, v in self.books.items()},
                "book_dispersion": round(self.spread_between_books, 4), "newest": self.newest,
                "oldest": self.oldest, "sources": self.sources}


def latest_per_book(quotes: list[Quote]) -> dict:
    """{(book, market, team, line): newest Quote} across all sources."""
    best: dict = {}
    for q in quotes:
        k = (q.book, q.market, normalize_team(q.team), q.line)
        if k not in best or q.updated > best[k].updated:
            best[k] = q
    return best


def consensus(quotes: list[Quote], market: str, side_team: str, line: Optional[float],
              other_team: str = "", books: Optional[list[str]] = None, method: str = "power",
              max_age_s: float = 3600, now: Optional[float] = None, draw: bool = False) -> Optional[Fair]:
    """Fair probability that `side_team` (or 'over'/'under') wins the given market at `line`.

    Requires each book to quote BOTH sides at the exact line (opposite sign for spreads)."""
    now = now or time.time()
    latest = latest_per_book(quotes)
    side_n, other_n = normalize_team(side_team), normalize_team(other_team)
    if market == "total":
        other_n = "under" if side_n == "over" else "over"
    per_book: dict = {}
    used: list[Quote] = []
    for book in sorted({k[0] for k in latest}):
        if books and book not in books:
            continue
        if market == "spread":
            a = latest.get((book, market, side_n, line))
            b = latest.get((book, market, other_n, -line if line is not None else None))
        elif market == "total":
            a = latest.get((book, market, side_n, line))
            b = latest.get((book, market, other_n, line))
        else:
            a = latest.get((book, market, side_n, None))
            b = latest.get((book, market, other_n, None))
        if not a or not b or now - min(a.updated, b.updated) > max_age_s:
            continue
        legs = [a.implied, b.implied]
        if market == "moneyline" and draw:
            d = latest.get((book, market, "draw", None))
            if not d:
                continue
            legs.append(d.implied)
        per_book[book] = devig(legs, method)[0]
        used += [a, b]
    if not per_book:
        return None
    vals = list(per_book.values())
    return Fair(prob=sum(vals) / len(vals), n_books=len(vals), books=per_book,
                spread_between_books=max(vals) - min(vals), newest=max(q.updated for q in used),
                oldest=min(q.updated for q in used), sources=sorted({q.source for q in used}))


def main_line(quotes: list[Quote], market: str, team_or_over: str) -> Optional[float]:
    """Most common line quoted for a side across books (the sportsbooks' main line)."""
    n = normalize_team(team_or_over)
    lines = [q.line for q in latest_per_book(quotes).values()
             if q.market == market and normalize_team(q.team) == n and q.line is not None]
    if not lines:
        return None
    return max(set(lines), key=lines.count)


# ---------------------------------------------------------------------------- budgets
class Budget:
    """Hard usage caps. Local ledger (persisted) + provider headers; never exceeds the cap."""

    def __init__(self, name: str, store, limits: dict):
        self.name = name
        self.store = store                     # TradeStore-like with get_state/set_state, or None
        self.limits = limits                   # {"day": n, "month": n, "reserve": n}
        self.state = (store.get_state(f"budget:{name}") if store else None) or {}
        self.remaining_header: Optional[float] = None

    def _roll(self, now: float) -> None:
        d = datetime.fromtimestamp(now, tz=timezone.utc)
        day, month = d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")
        if self.state.get("day") != day:
            self.state["day"], self.state["day_used"] = day, 0
        if self.state.get("month") != month:
            self.state["month"], self.state["month_used"] = month, 0

    def can_spend(self, cost: float, now: Optional[float] = None) -> bool:
        now = now or time.time()
        self._roll(now)
        reserve = float(self.limits.get("reserve", 0))
        if self.remaining_header is not None and self.remaining_header - cost < reserve:
            return False
        if "day" in self.limits and self.state["day_used"] + cost > self.limits["day"] - reserve:
            return False
        if "month" in self.limits and self.state["month_used"] + cost > self.limits["month"] - reserve:
            return False
        return True

    def spend(self, cost: float, remaining_header: Optional[str] = None, now: Optional[float] = None) -> None:
        now = now or time.time()
        self._roll(now)
        self.state["day_used"] += cost
        self.state["month_used"] += cost
        if remaining_header not in (None, ""):
            try:
                self.remaining_header = float(remaining_header)
            except ValueError:
                pass
        if self.store:
            self.store.set_state(f"budget:{self.name}", self.state)

    def summary(self) -> dict:
        self._roll(time.time())
        return {"day_used": self.state.get("day_used", 0), "month_used": self.state.get("month_used", 0),
                "limits": self.limits, "provider_remaining": self.remaining_header}


class OddsSource:
    """Interface: fetch_league(league) -> list[OddsEvent]. Swap/upgrade providers in config."""

    name = "base"
    leagues: dict = {}

    def __init__(self, conf: dict, store=None, client: Optional[httpx.AsyncClient] = None):
        self.conf = conf
        self.client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "btc-signals/1.0"})
        self.budget = Budget(self.name, store, conf.get("budget", {}))
        self.key = os.environ.get(conf.get("key_env", ""), "").strip()
        self.last_error = ""
        self._lock_obj = None               # created on first use: Python 3.9 binds it to a loop
        self._last_call = 0.0
        self.min_interval_s = float(conf.get("min_interval_s", 1.0))

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def available(self) -> bool:
        return bool(self.key) and self.conf.get("enabled", True)

    async def _throttle(self) -> None:
        wait = self._last_call + self.min_interval_s - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.time()

    async def fetch_league(self, league: str) -> list[OddsEvent]:
        return []

    def summary(self) -> dict:
        return {"name": self.name, "available": self.available, "budget": self.budget.summary(),
                "error": self.last_error}


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------- TheRundown
class TheRundownSource(OddsSource):
    name = "therundown"
    SPORT_IDS = {"nfl": 2, "nba": 4, "nhl": 6, "epl": 11, "mlb": 3, "cfb": 1, "cbb": 5, "mls": 10,
                 "ucl": 16, "wnba": 8}
    BOOKS = {"19": "draftkings", "23": "fanduel", "22": "betmgm"}
    MARKETS = {1: "moneyline", 2: "spread", 3: "total"}

    @staticmethod
    def parse_events(d: dict, league: str, now: float) -> list[OddsEvent]:
        out = []
        for e in d.get("events") or []:
            teams = e.get("teams") or []
            if len(teams) < 2:
                continue
            away = teams[0].get("name", "")
            home = teams[1].get("name", "")
            ev = OddsEvent("therundown", str(e.get("event_id")), league, home, away,
                           _iso(e["event_date"]) if e.get("event_date") else None)
            status = ((e.get("score") or {}).get("event_status") or "")
            ev.status = status  # type: ignore[attr-defined]
            for mk in e.get("markets") or []:
                market = TheRundownSource.MARKETS.get(mk.get("market_id"))
                if market is None or int(mk.get("period_id") or 0) != 0:
                    continue
                for part in mk.get("participants") or []:
                    pname = part.get("name") or ""
                    if market == "total":
                        pname = "over" if "over" in pname.lower() else "under" if "under" in pname.lower() else pname
                    elif part.get("type") == "TYPE_RESULT" and "draw" in pname.lower():
                        pname = "draw"
                    for ln in part.get("lines") or []:
                        val = ln.get("value")
                        line = float(val) if val not in (None, "") and market != "moneyline" else None
                        for aff, pr in (ln.get("prices") or {}).items():
                            book = TheRundownSource.BOOKS.get(str(aff))
                            if not book or pr.get("price") in (None, 0, 0.0):
                                continue
                            if market != "moneyline" and not pr.get("is_main_line", True):
                                continue
                            ev.quotes.append(Quote("therundown", book, market, pname, line,
                                                   float(pr["price"]),
                                                   _iso(pr["updated_at"]) if pr.get("updated_at") else now, now))
            out.append(ev)
        return out

    async def fetch_league(self, league: str, days_ahead: int = 1) -> list[OddsEvent]:
        sid = self.SPORT_IDS.get(league)
        if sid is None or not self.available:
            return []
        out: list[OddsEvent] = []
        base = self.conf.get("url", "https://therundown.io/api/v2")
        for d in range(days_ahead + 1):
            date = datetime.fromtimestamp(time.time() + d * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
            est = 25 * 15                                      # rough pre-check: 15 games x 25 points
            if not self.budget.can_spend(est):
                self.last_error = "data-point budget reserve reached; skipping refresh"
                return out
            async with self._lock:
                await self._throttle()
                r = await self.client.get(f"{base}/sports/{sid}/events/{date}",
                                          params={"market_ids": "1,2,3", "affiliate_ids": "19,22,23",
                                                  "main_line": "true", "offset": "0"},
                                          headers={"X-TheRundown-Key": self.key})
            cost = float(r.headers.get("X-Datapoints") or 0)
            self.budget.spend(cost, r.headers.get("X-Datapoints-Remaining"))
            if r.status_code == 429:
                self.last_error = "rate limited / quota paused"
                return out
            r.raise_for_status()
            out += self.parse_events(r.json(), league, time.time())
        self.last_error = ""
        return out


# ---------------------------------------------------------------------------- SharpAPI
class SharpApiSource(OddsSource):
    name = "sharpapi"
    LEAGUES = {"nfl": "nfl", "nba": "nba", "nhl": "nhl", "epl": "epl", "mlb": "mlb", "cfb": "ncaaf",
               "cbb": "ncaab", "mls": "mls", "ucl": "ucl", "wnba": "wnba"}
    MARKETS = {"moneyline": "moneyline", "point_spread": "spread", "spread": "spread", "puck_line": "spread",
               "total_points": "total", "total_goals": "total", "total": "total"}

    @staticmethod
    def parse_odds(d: dict, league: str, now: float) -> list[OddsEvent]:
        events: dict = {}
        for o in d.get("data") or []:
            market = SharpApiSource.MARKETS.get(o.get("market_type", ""))
            if market is None or o.get("is_main_line") is False:
                continue
            eid = str(o.get("event_id") or "")
            ev = events.get(eid)
            if ev is None:
                ev = events[eid] = OddsEvent("sharpapi", eid, league, o.get("home_team", ""),
                                             o.get("away_team", ""),
                                             _iso(o["start_time"]) if o.get("start_time") else None)
            sel = (o.get("selection") or "").strip()
            stype = (o.get("selection_type") or "").lower()
            if market == "total":
                sel = "over" if (stype or sel.lower()).startswith("o") else "under"
            elif stype == "draw" or sel.lower() == "draw":
                sel = "draw"
            line = o.get("line") if market != "moneyline" else None
            ts = o.get("timestamp") or o.get("updated_at")
            ev.quotes.append(Quote("sharpapi", str(o.get("sportsbook", "")).lower(), market, sel,
                                   float(line) if line is not None else None, float(o["odds_american"]),
                                   _iso(ts) if ts else now, now))
        return list(events.values())

    async def fetch_league(self, league: str) -> list[OddsEvent]:
        lg = self.LEAGUES.get(league)
        if lg is None or not self.available:
            return []
        rows: list = []
        offset, pages = 0, int(self.conf.get("max_pages", 4))
        for _ in range(pages):
            async with self._lock:
                await self._throttle()                    # 12/min -> min_interval_s >= 5
                r = await self.client.get(self.conf.get("url", "https://api.sharpapi.io/api/v1") + "/odds",
                                          params={"league": lg, "market": "main", "is_main_line": "true",
                                                  "limit": 200, "offset": offset},
                                          headers={"X-API-Key": self.key})
            self.budget.spend(1, r.headers.get("X-RateLimit-Remaining"))
            if r.status_code == 429:
                self.last_error = "rate limited"
                break
            r.raise_for_status()
            d = r.json()
            rows += d.get("data") or []
            pg = (d.get("meta") or {}).get("pagination") or {}
            if not pg.get("has_more"):
                break
            offset = pg.get("next_offset", offset + 200)
        self.last_error = ""
        return self.parse_odds({"data": rows}, league, time.time())


# ---------------------------------------------------------------------------- The Odds API
class TheOddsApiSource(OddsSource):
    """Pinnacle sanity check only. Spends credits only when asked, never past the cap."""

    name = "theoddsapi"
    SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "nhl": "icehockey_nhl",
                  "epl": "soccer_epl", "mlb": "baseball_mlb", "cfb": "americanfootball_ncaaf",
                  "cbb": "basketball_ncaab", "mls": "soccer_usa_mls", "ucl": "soccer_uefa_champs_league"}
    MARKET_KEYS = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}

    def __init__(self, conf, store=None, client=None):
        super().__init__(conf, store, client)
        self._events: dict = {}                            # league -> (fetched, list)

    async def list_events(self, league: str) -> list[dict]:
        """Free endpoint (does not count against the quota); cached 30 min."""
        cached = self._events.get(league)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]
        key = self.SPORT_KEYS.get(league)
        if key is None or not self.available:
            return []
        async with self._lock:
            await self._throttle()
            r = await self.client.get(f"https://api.the-odds-api.com/v4/sports/{key}/events",
                                      params={"apiKey": self.key})
        r.raise_for_status()
        evs = r.json()
        self._events[league] = (time.time(), evs)
        return evs

    @staticmethod
    def parse_event_odds(d: dict, league: str, now: float) -> OddsEvent:
        ev = OddsEvent("theoddsapi", d.get("id", ""), league, d.get("home_team", ""), d.get("away_team", ""),
                       _iso(d["commence_time"]) if d.get("commence_time") else None)
        inv = {v: k for k, v in TheOddsApiSource.MARKET_KEYS.items()}
        for bk in d.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                market = inv.get(mk.get("key"))
                if not market:
                    continue
                upd = _iso(mk.get("last_update") or bk.get("last_update")) if (mk.get("last_update") or bk.get("last_update")) else now
                for oc in mk.get("outcomes") or []:
                    name = oc.get("name", "")
                    if market == "total":
                        name = name.lower()
                    elif name.lower() == "draw":
                        name = "draw"
                    ev.quotes.append(Quote("theoddsapi", bk.get("key", ""), market, name,
                                           float(oc["point"]) if oc.get("point") is not None else None,
                                           float(oc["price"]), upd, now))
        return ev

    async def pinnacle_check(self, league: str, event_id: str, market: str) -> Optional[OddsEvent]:
        """1 credit (1 market x 1 'region' = bookmakers=pinnacle). Refuses if over budget."""
        key, mkey = self.SPORT_KEYS.get(league), self.MARKET_KEYS.get(market)
        if not (key and mkey and self.available):
            return None
        if not self.budget.can_spend(1):
            self.last_error = "credit reserve reached; Pinnacle checks paused until the monthly reset"
            return None
        async with self._lock:
            await self._throttle()
            r = await self.client.get(f"https://api.the-odds-api.com/v4/sports/{key}/events/{event_id}/odds",
                                      params={"apiKey": self.key, "bookmakers": "pinnacle", "markets": mkey,
                                              "oddsFormat": "american"})
        self.budget.spend(float(r.headers.get("x-requests-last") or 1), r.headers.get("x-requests-remaining"))
        if r.status_code in (401, 429):
            self.last_error = f"HTTP {r.status_code}"
            return None
        r.raise_for_status()
        self.last_error = ""
        return self.parse_event_odds(r.json(), league, time.time())


SOURCE_TYPES = {c.name: c for c in (TheRundownSource, SharpApiSource, TheOddsApiSource)}


def build_sources(conf: dict, store=None, client=None) -> dict:
    out = {}
    for name, c in (conf or {}).items():
        if name in SOURCE_TYPES and c.get("enabled", True):
            out[name] = SOURCE_TYPES[name](c, store, client)
    return out


ABBREV = {"la": "los angeles", "ny": "new york", "man": "manchester", "utd": "united",
          "st": "saint", "nyr": "new york rangers", "nyi": "new york islanders"}


def _tokens(name: str) -> set:
    out = set()
    for t in normalize_team(name).split():
        out.update(ABBREV.get(t, t).split())
    return out


def team_score(a: str, b: str) -> float:
    """Similarity of two team names: shared words, with the nickname (last word) weighted."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb) / max(len(ta), len(tb))
    na, nb = normalize_team(a).split()[-1], normalize_team(b).split()[-1]
    return shared + (0.5 if na == nb else 0.0)


def map_teams(names: list[str], targets: list[str], min_score: float = 0.5) -> Optional[dict]:
    """Map each of `names` to exactly one of `targets` (best unique assignment) or None."""
    if len(names) != len(targets) or not names:
        return None
    best, best_s = None, -1.0
    import itertools
    for perm in itertools.permutations(targets):
        sc = [team_score(n, t) for n, t in zip(names, perm)]
        if min(sc) >= min_score and sum(sc) > best_s:
            best, best_s = dict(zip(names, perm)), sum(sc)
    return best


def canonicalize(quotes: list[Quote], mapping: dict) -> list[Quote]:
    """Rename quote teams to the venue's full team names (totals/draw untouched)."""
    out = []
    for q in quotes:
        if q.team in ("over", "under", "draw"):
            out.append(q)
        elif q.team in mapping:
            out.append(Quote(q.source, q.book, q.market, mapping[q.team], q.line, q.american, q.updated, q.fetched))
    return out


def match_event(events: list[OddsEvent], team_names: list[str], starts_at: Optional[float],
                max_time_diff_s: float = 3 * 3600) -> Optional[OddsEvent]:
    """Same two teams (word-overlap matching, unique assignment) and a close start time."""
    best = None
    for e in events:
        if map_teams([e.home, e.away], list(team_names)) is None:
            continue
        if starts_at and e.starts_at and abs(e.starts_at - starts_at) > max_time_diff_s:
            continue
        if best is None or (starts_at and e.starts_at and abs(e.starts_at - starts_at) <
                            abs((best.starts_at or 0) - starts_at)):
            best = e
    return best
