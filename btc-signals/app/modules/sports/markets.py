"""Polymarket US sports markets: discovery, classification, rules check, liquidity filter.

Verified against the live gateway (2026-11-09):
  GET /v2/leagues/{league}/events          games (one moneyline each in the listing)
  GET /v1/events/slug/{slug}               every market for one game (NBA: ~640)
  market.sportsMarketType                  e.g. basketball_team_full_game_winner / _spread / _total
  market.marketSides[i].long / .team.name  long side = first team (moneyline), the team whose
                                           handicap `line` is (spread), Over (total)
  market.description                       settlement text (overtime, postponement -> LFMP)
Only full-game moneyline, spread and total are used (no props, no periods).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ...ingest.us_markets import long_token, short_token

# sportsMarketType suffix -> bet type (full game only)
BET_TYPES = {"team_full_game_winner": "moneyline", "team_full_game_spread": "spread",
             "team_full_game_total": "total"}
SPORT_OF_LEAGUE = {"nfl": "football", "cfb": "football", "nba": "basketball", "cbb": "basketball",
                   "wnba": "basketball", "nhl": "hockey", "mlb": "baseball", "epl": "soccer",
                   "ucl": "soccer", "mls": "soccer"}


def _ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class SportsMarket:
    slug: str
    event_slug: str
    league: str
    bet_type: str                    # moneyline | spread | total
    line: Optional[float]            # handicap for the LONG side (spread) or total points
    long_label: str                  # "Lakers" / "+1.50" / "Over"
    short_label: str
    long_team: str = ""              # full team name for moneyline/spread long side
    short_team: str = ""
    home_team: str = ""
    away_team: str = ""
    starts_at: Optional[float] = None
    fee_schedule: dict = field(default_factory=dict)
    tick_size: float = 0.01
    min_size: float = 0.01
    status: str = ""
    rules_ok: bool = False
    rules_issues: list = field(default_factory=list)
    description: str = ""

    @property
    def token_long(self) -> str:
        return long_token(self.slug)

    @property
    def token_short(self) -> str:
        return short_token(self.slug)

    def side_label(self, token: str) -> str:
        if self.bet_type == "moneyline":
            return self.long_label if token.endswith(":long") else self.short_label
        if self.bet_type == "spread":
            team = self.long_team if token.endswith(":long") else self.short_team
            line = self.line if token.endswith(":long") else -(self.line or 0)
            return f"{team.split()[-1]} {line:+g}"
        return f"{'Over' if token.endswith(':long') else 'Under'} {self.line:g}"

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "description"}


def check_rules(m: dict, sport: str) -> list[str]:
    """Return problems with the settlement text; empty list = understood. Unknown -> skip."""
    d = (m.get("description") or "").lower()
    issues = []
    if not d:
        return ["no rules text"]
    if sport == "soccer":
        if "regulation" not in d and "90 minutes" not in d:
            issues.append("soccer market does not state regulation-time settlement")
    else:
        if "overtime is included" not in d and "overtime included" not in d:
            issues.append("rules do not say overtime is included")
    if "last fair market price" not in d:
        issues.append("rules do not state how postponements are settled")
    t = m.get("sportsMarketType") or ""
    if t.endswith("_spread") and "covers a" not in d:
        issues.append("spread text not in the expected 'covers a X point spread' form")
    if t.endswith("_total") and "combine for over" not in d:
        issues.append("total text not in the expected 'combine for over X' form")
    if t.endswith("_winner") and "settle to the winner" not in d:
        issues.append("moneyline text not in the expected 'settle to the winner' form")
    return issues


def classify_market(m: dict, event: dict, league: str) -> Optional[SportsMarket]:
    """Turn a gateway market into a SportsMarket, or None if it's not a full-game ML/spread/total."""
    t = m.get("sportsMarketType") or ""
    bet = next((v for k, v in BET_TYPES.items() if t.endswith(k)), None)
    if bet is None:
        return None
    sides = m.get("marketSides") or []
    longs = [s for s in sides if s.get("long")]
    shorts = [s for s in sides if not s.get("long")]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    teams = event.get("teams") or []
    home = next((x.get("name", "") for x in teams if x.get("home") or x.get("isHome")), "")
    away = next((x.get("name", "") for x in teams if x.get("away") or x.get("isAway")), "")
    try:
        line = float(m["line"]) if m.get("line") not in (None, "") else None
    except (TypeError, ValueError):
        line = None
    sport = SPORT_OF_LEAGUE.get(league, "")
    sm = SportsMarket(
        slug=m["slug"], event_slug=event.get("slug", ""), league=league, bet_type=bet, line=line,
        long_label=longs[0].get("description", ""), short_label=shorts[0].get("description", ""),
        long_team=(longs[0].get("team") or {}).get("name", ""), short_team=(shorts[0].get("team") or {}).get("name", ""),
        home_team=home, away_team=away, starts_at=_ts(event.get("startDate") or event.get("startTime")),
        fee_schedule={"rate": float(m.get("feeCoefficient") or 0.095), "exponent": 1, "takerOnly": True},
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01), min_size=float(m.get("minimumTradeQty") or 1),
        status=m.get("status", ""), description=m.get("description") or "")
    if bet in ("spread", "total") and line is None:
        sm.rules_issues = ["no line"]
    else:
        sm.rules_issues = check_rules(m, sport)
    sm.rules_ok = not sm.rules_issues and sm.status == "MARKET_STATUS_OPEN"
    return sm


def liquidity_ok(book: Optional[dict], max_spread: float, min_depth: float) -> tuple[bool, str]:
    """Best bid/ask must exist, spread <= max_spread, and the best level on each side >= min_depth."""
    if not book or not book.get("bids") or not book.get("asks"):
        return False, "empty book"
    bid, bq = book["bids"][0]
    ask, aq = book["asks"][0]
    if ask - bid > max_spread + 1e-9:
        return False, f"spread {ask - bid:.2f} > {max_spread:.2f}"
    if min(bq, aq) < min_depth:
        return False, f"top-of-book depth {min(bq, aq):.0f} < {min_depth:.0f}"
    return True, ""


def normalize_team(name: str) -> str:
    """Lower-case, strip punctuation and common suffixes for cross-source matching."""
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    n = re.sub(r"\b(fc|afc|cf|sc)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class SportsGame:
    event_slug: str
    league: str
    title: str
    starts_at: Optional[float]
    home_team: str = ""
    away_team: str = ""
    team_names: list = field(default_factory=list)   # full names from the market sides
    markets: dict = field(default_factory=dict)       # slug -> SportsMarket
    fetched: float = 0.0
    state: dict = field(default_factory=dict)

    def started(self, now: float) -> bool:
        return self.starts_at is not None and now >= self.starts_at


def parse_event(event: dict, league: str) -> SportsGame:
    g = SportsGame(event_slug=event.get("slug", ""), league=league, title=event.get("title", ""),
                   starts_at=_ts(event.get("startDate") or event.get("startTime")),
                   state=event.get("eventState") or {}, fetched=time.time())
    names: list[str] = []
    for m in event.get("markets") or []:
        sm = classify_market(m, event, league)
        if sm is None:
            continue
        g.markets[sm.slug] = sm
        for n in (sm.long_team, sm.short_team):
            if n and n not in names:
                names.append(n)
    g.team_names = names
    return g
