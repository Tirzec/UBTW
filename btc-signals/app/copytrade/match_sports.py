"""Map an international Polymarket sports market (+ the outcome a copied trader bought) to the SAME market and
side on Polymarket US. Strict: same game (kickoff to the second, both teams by provider id), same market type,
same line incl. sign for the backed team, half-point lines only. Never moves to a 'nearest' line.

Pure functions (unit-tested) + an async resolver that fetches what they need. Read-only: no orders here.

Facts this relies on (verified live 2026-11-10 against NFL/CFB/NBA events):
- gamma event: usId (US event slug, often missing for CFB), startTime (ISO Z), teams[{name, alias,
  abbreviation, providerId, ordering home|away}]
- gamma market: sportsMarketType moneyline|spreads|totals, line (handicap of outcomes[0]), outcomes (JSON)
- US event /v1/events/slug/{slug}: startTime, gameId, markets[{slug, sportsMarketType "*_team_full_game_*",
  line, gameStartTime, status, marketSides[{long, description, team{providerId, ordering, ...}}]}]
- US slugs use the ET date of kickoff; intl NFL slugs use the UTC date (night games differ by a day)
- US event.teams is unreliable (mis-tagged props): take teams from the moneyline market's sides
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

LEAGUES = ("nfl", "cfb", "nba", "cbb", "nhl", "mlb", "wnba")
# intl team code -> US team code where they differ (seen live). Unknown ones are found by kickoff time instead.
CODE_ALIASES = {"nba": {"nop": "no", "sas": "sa", "nyk": "ny", "gsw": "gs"}, "nfl": {"la": "lar"}}


class NoMatch(Exception):
    """Not copyable on Polymarket US (the message says why)."""


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _type(m: dict) -> str:
    return m.get("sportsMarketType") or ""


def _parse_iso(s: str | None) -> Optional[float]:
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def et_date(ts: float) -> date:
    """US Eastern calendar date (DST: second Sunday of March .. first Sunday of November)."""
    u = datetime.fromtimestamp(ts, timezone.utc)
    y = u.year
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)                       # 2 am EST = 7 UTC
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)                      # 2 am EDT = 6 UTC
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    off = -4 if dst_start <= u < dst_end else -5
    return (u + timedelta(hours=off)).date()


def team_match(it: dict, ut: Optional[dict]) -> bool:
    """Same team: provider id AND (abbreviation or a name/alias) agree."""
    if not ut or it.get("providerId") is None:
        return False
    if str(it["providerId"]) != str(ut.get("providerId")):
        return False
    names_i = {norm(it.get("name")), norm(it.get("alias"))} - {""}
    names_u = {norm(ut.get("name")), norm(ut.get("alias")),
               norm((ut.get("safeName") or "").split(" ", 1)[-1])} - {""}
    return norm(it.get("abbreviation")) == norm(ut.get("abbreviation")) or bool(names_i & names_u)


def us_event_candidates(iev: dict) -> list[str]:
    """US event slugs to try, best first."""
    out = []
    if iev.get("usId"):
        out.append(iev["usId"])
    mm = re.match(r"^(.*)-(\d{4}-\d{2}-\d{2})$", iev.get("slug") or "")
    if mm:
        base, d0 = mm.group(1), date.fromisoformat(mm.group(2))
        st = _parse_iso(iev.get("startTime"))
        days = ([et_date(st)] if st else []) + [d0, d0 - timedelta(days=1), d0 + timedelta(days=1)]
        parts = base.split("-")
        bases = [base]
        if len(parts) == 3:
            al = CODE_ALIASES.get(parts[0], {})
            alt = "-".join([parts[0], al.get(parts[1], parts[1]), al.get(parts[2], parts[2])])
            if alt != base:
                bases.insert(0, alt)
        out += [f"{b}-{d.isoformat()}" for b in bases for d in days]
    return list(dict.fromkeys(out))


def us_game_teams(ue: dict) -> dict:
    ml = [x for x in ue.get("markets") or [] if _type(x).endswith("_team_full_game_winner")]
    if not ml:
        return {}
    return {(s.get("team") or {}).get("ordering"): s.get("team") for s in ml[0].get("marketSides") or []
            if s.get("team")}


def verify_game(iev: dict, ue: dict) -> Optional[str]:
    """None if the US event is the same game, else the reason it is not."""
    a, b = _parse_iso(iev.get("startTime")), _parse_iso(ue.get("startTime"))
    if a is None or b is None or abs(a - b) > 1:
        return f"kickoff differs ({iev.get('startTime')} vs {ue.get('startTime')})"
    it = {t.get("ordering"): t for t in iev.get("teams") or []}
    if set(it) < {"home", "away"}:
        return "intl event has no home/away teams"
    ut = us_game_teams(ue)
    if not all(team_match(it[k], ut.get(k)) for k in ("home", "away")):
        return "teams differ"
    return None


def _outcome_team(it: dict, label: str) -> dict:
    c = [x for x in it.values() if norm(label) in (norm(x.get("alias")), norm(x.get("name")))]
    if len(c) != 1:
        c = [x for x in it.values() if norm(label) and norm(x.get("name"))
             and (norm(label) in norm(x.get("name")) or norm(x.get("name")) in norm(label))]
    if len(c) != 1:
        raise NoMatch(f"cannot tell which team '{label}' is")
    return c[0]


def map_market(m: dict, iev: dict, ue: dict, outcome_index: int, outcome: str) -> dict:
    """Intl market + bought outcome -> {"us_slug", "leg" long|short, "token", "label", "bet_type",
    "start_ts", "line"}. Raises NoMatch."""
    why = verify_game(iev, ue)
    if why:
        raise NoMatch(why)
    outs = json.loads(m.get("outcomes") or "[]")
    if not 0 <= outcome_index < len(outs) or outs[outcome_index] != outcome:
        raise NoMatch("outcome does not match the market's outcomes")
    it = {t.get("ordering"): t for t in iev.get("teams") or []}
    smt = m.get("sportsMarketType")
    markets = ue.get("markets") or []
    line = None
    if smt == "moneyline":
        bt = "moneyline"
        team = _outcome_team(it, outcome)
        cands = [x for x in markets if _type(x).endswith("_team_full_game_winner")]
        want = lambda s: team_match(team, s.get("team"))  # noqa: E731
    elif smt == "spreads":
        bt = "spread"
        line = float(m["line"])                                  # handicap of outcomes[0]
        team = _outcome_team(it, outcome)
        h = line if outcome_index == 0 else -line
        cands = [x for x in markets if _type(x).endswith("_team_full_game_spread")]

        def want(s):
            try:
                return team_match(team, s.get("team")) and abs(float(s.get("description")) - h) < 1e-9
            except (TypeError, ValueError):
                return False
    elif smt == "totals":
        bt = "total"
        line = float(m["line"])
        cands = [x for x in markets if _type(x).endswith("_team_full_game_total") and x.get("line") is not None
                 and abs(float(x["line"]) - line) < 1e-9]
        want = lambda s: s.get("description") == outcome  # noqa: E731
    else:
        raise NoMatch(f"market type '{smt}' is not copied (full-game moneyline/spread/total only)")
    if line is not None and abs(line - round(line)) < 1e-9:
        raise NoMatch("whole-number line (a push is possible and settles differently)")
    hits = [(x, s) for x in cands for s in x.get("marketSides") or [] if want(s)]
    if len(hits) != 1:
        raise NoMatch("that line is not listed on Polymarket US" if not hits else "ambiguous US market")
    x, s = hits[0]
    gs = _parse_iso(x.get("gameStartTime"))
    if gs is not None and abs(gs - (_parse_iso(ue.get("startTime")) or 0)) > 1:
        raise NoMatch("US market start time differs from the game")
    leg = "long" if s.get("long") else "short"
    return {"us_slug": x["slug"], "leg": leg, "token": f"{x['slug']}:{leg}", "bet_type": bt,
            "label": f"{s.get('description') or outcome}" + (f" ({bt})" if bt != "moneyline" else ""),
            "start_ts": _parse_iso(ue.get("startTime")), "line": line, "status": x.get("status")}


def league_of(slug: str) -> Optional[str]:
    p = (slug or "").split("-", 1)[0]
    return p if p in LEAGUES else None


async def resolve_us_event(iev: dict, gw: Callable[[str], Awaitable[Optional[dict]]]) -> tuple[Optional[dict], str]:
    """(US event, slug) for an intl event, or (None, reason). gw(path) -> JSON or None (404)."""
    last = "no US event for this game"
    for s in us_event_candidates(iev):
        d = await gw(f"/v1/events/slug/{s}")
        ue = (d or {}).get("event") if isinstance(d, dict) else None
        if not ue:
            continue
        why = verify_game(iev, ue)
        if why is None:
            return ue, s
        last = f"US event {s}: {why}"
    lg = league_of(iev.get("slug") or "")
    st = _parse_iso(iev.get("startTime"))
    if lg and st:                                   # unknown team code: find the open game by kickoff time
        d = await gw(f"/v2/leagues/{lg}/events?limit=100")
        for e in (d or {}).get("events") or []:
            es = _parse_iso(e.get("startTime") or e.get("startDate"))
            if es is not None and abs(es - st) <= 1 and e.get("slug"):
                full = await gw(f"/v1/events/slug/{e['slug']}")
                ue = (full or {}).get("event") if isinstance(full, dict) else None
                if ue and verify_game(iev, ue) is None:
                    return ue, e["slug"]
    return None, last
