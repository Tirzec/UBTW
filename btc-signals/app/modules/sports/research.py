"""Free research inputs for the sports module, each cached to respect rate limits.

Sources (all free, no key):
  ESPN site API  site.api.espn.com   injuries, news, scoreboards (venue, indoor flag, records).
                 Unofficial/undocumented: may change without notice, so every call is optional.
  Open-Meteo     api.open-meteo.com / geocoding-api.open-meteo.com   weather at the venue.
  MLB Stats API  statsapi.mlb.com    probable pitchers (MLB is off by default).
Design rule: research never invents a probability. It produces dated Reasons for the research
card and two kinds of flags: `stale_line` (relevant news newer than our newest odds quote, so
the line may not reflect it yet: block until odds refresh) and informational flags (weather,
back-to-back, rest)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..base import Reason
from .odds import team_score

log = logging.getLogger("sports.research")

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_PATH = {"nfl": "football/nfl", "nba": "basketball/nba", "nhl": "hockey/nhl", "epl": "soccer/eng.1",
             "mlb": "baseball/mlb", "cfb": "football/college-football", "cbb": "basketball/mens-college-basketball",
             "wnba": "basketball/wnba", "mls": "soccer/usa.1", "ucl": "soccer/uefa.champions"}
OUTDOOR_SPORTS = {"nfl", "cfb", "mlb", "epl", "mls", "ucl"}
NEWS_TERMS = {"ruled out": 1.0, "will not play": 1.0, "out for": 0.9, "season-ending": 1.0, "suspended": 0.9,
              "doubtful": 0.6, "questionable": 0.4, "game-time decision": 0.5, "scratched": 0.9,
              "starting": 0.4, "will start": 0.5, "benched": 0.6, "injury": 0.3, "illness": 0.4,
              "concussion": 0.8, "torn": 1.0, "fracture": 0.9, "rest": 0.4, "load management": 0.6}
STATUS_WEIGHT = {"out": 1.0, "doubtful": 0.6, "questionable": 0.35, "day-to-day": 0.25, "suspension": 1.0}


def _iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Injury:
    team: str
    player: str
    status: str
    date: Optional[float]
    comment: str = ""

    @property
    def weight(self) -> float:
        return STATUS_WEIGHT.get(self.status.lower(), 0.2)


@dataclass
class GameResearch:
    reasons: list = field(default_factory=list)       # Reason objects for the research card
    flags: list = field(default_factory=list)         # short strings
    newest_news_ts: float = 0.0                       # newest relevant injury/news timestamp
    injuries: list = field(default_factory=list)
    weather: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"reasons": [r.as_dict() for r in self.reasons], "flags": self.flags,
                "newest_news_ts": self.newest_news_ts,
                "injuries": [i.__dict__ for i in self.injuries][:12], "weather": self.weather}


def et_date(ts: float) -> datetime:
    """Calendar date in US Eastern time (ESPN's `dates=` key)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - no tz database: approximate with UTC-5
        return datetime.fromtimestamp(ts - 5 * 3600, tz=timezone.utc)


def parse_espn_injuries(d: dict) -> list[Injury]:
    out = []
    for t in d.get("injuries") or []:
        team = t.get("displayName", "")
        for i in t.get("injuries") or []:
            out.append(Injury(team, (i.get("athlete") or {}).get("displayName", ""), i.get("status", ""),
                              _iso(i.get("date")), (i.get("shortComment") or "")[:200]))
    return out


def score_headline(text: str) -> float:
    t = text.lower()
    return min(1.0, sum(w for k, w in NEWS_TERMS.items() if k in t))


def parse_espn_news(d: dict) -> list[dict]:
    out = []
    for a in d.get("articles") or []:
        title = a.get("headline") or ""
        desc = a.get("description") or ""
        out.append({"title": title, "desc": desc, "ts": _iso(a.get("published")) or 0.0,
                    "url": ((a.get("links") or {}).get("web") or {}).get("href", ""),
                    "impact": score_headline(title + " " + desc)})
    return out


def weather_flags(hourly: dict, at: float) -> dict:
    """Pick the forecast hour nearest kickoff; flag wind >= 25 km/h, rain >= 1 mm/h, temp <= -5 C."""
    times = hourly.get("time") or []
    if not times:
        return {}
    ts = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp() for x in times]
    i = min(range(len(ts)), key=lambda k: abs(ts[k] - at))
    w = {"temp_c": (hourly.get("temperature_2m") or [None])[i], "wind_kmh": (hourly.get("wind_speed_10m") or [None])[i],
         "precip_mm": (hourly.get("precipitation") or [None])[i]}
    flags = []
    if (w["wind_kmh"] or 0) >= 25:
        flags.append(f"wind {w['wind_kmh']:.0f} km/h")
    if (w["precip_mm"] or 0) >= 1:
        flags.append(f"precipitation {w['precip_mm']:.1f} mm/h")
    if w["temp_c"] is not None and w["temp_c"] <= -5:
        flags.append(f"cold {w['temp_c']:.0f}°C")
    w["flags"] = flags
    return w


class Research:
    def __init__(self, conf: dict, client: Optional[httpx.AsyncClient] = None):
        self.conf = conf or {}
        self.client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "btc-signals/1.0"})
        self.cache: dict = {}                       # key -> (ts, value)
        self.errors: dict = {}

    async def _get(self, key: str, url: str, ttl: float, params: Optional[dict] = None):
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            r = await self.client.get(url, params=params)
            r.raise_for_status()
            val = r.json()
            self.cache[key] = (time.time(), val)
            self.errors.pop(key, None)
            return val
        except Exception as e:  # noqa: BLE001 - research is optional
            self.errors[key] = f"{type(e).__name__}: {e}"[:160]
            return hit[1] if hit else None

    async def injuries(self, league: str) -> list[Injury]:
        p = ESPN_PATH.get(league)
        d = await self._get(f"inj:{league}", f"{ESPN}/{p}/injuries", float(self.conf.get("injuries_ttl_s", 600))) if p else None
        return parse_espn_injuries(d) if d else []

    async def news(self, league: str) -> list[dict]:
        p = ESPN_PATH.get(league)
        d = await self._get(f"news:{league}", f"{ESPN}/{p}/news", float(self.conf.get("news_ttl_s", 300)),
                            {"limit": 50}) if p else None
        return parse_espn_news(d) if d else []

    async def scoreboard(self, league: str, day: datetime) -> dict:
        p = ESPN_PATH.get(league)
        if not p:
            return {}
        k = day.strftime("%Y%m%d")
        return await self._get(f"sb:{league}:{k}", f"{ESPN}/{p}/scoreboard", 1800, {"dates": k}) or {}

    async def weather(self, city: str, at: float) -> dict:
        if not city:
            return {}
        geo = await self._get(f"geo:{city}", "https://geocoding-api.open-meteo.com/v1/search", 86400 * 7,
                              {"name": city.split(",")[0], "count": 1})
        res = (geo or {}).get("results") or []
        if not res:
            return {}
        lat, lon = res[0]["latitude"], res[0]["longitude"]
        f = await self._get(f"wx:{lat:.2f},{lon:.2f}", "https://api.open-meteo.com/v1/forecast", 3600,
                            {"latitude": lat, "longitude": lon, "hourly": "temperature_2m,wind_speed_10m,precipitation",
                             "forecast_days": 3, "timezone": "UTC"})
        return weather_flags((f or {}).get("hourly") or {}, at)

    async def game(self, league: str, teams: list[str], starts_at: Optional[float]) -> GameResearch:
        """Everything we know about one game, as dated reasons and flags."""
        gr = GameResearch()
        now = time.time()
        # injuries
        for inj in await self.injuries(league):
            if not any(team_score(inj.team, t) >= 1.0 for t in teams):
                continue
            gr.injuries.append(inj)
            if inj.weight >= 0.35:
                gr.reasons.append(Reason(f"{inj.player} ({inj.team.split()[-1]}): {inj.status}. {inj.comment}",
                                         "ESPN injuries", -inj.weight, inj.date or 0))
                if inj.date and now - inj.date < 6 * 3600:
                    gr.newest_news_ts = max(gr.newest_news_ts, inj.date)
        # news mentioning either team (nickname match)
        nicks = [t.split()[-1].lower() for t in teams if t]
        for n in await self.news(league):
            text = (n["title"] + " " + n["desc"]).lower()
            if n["impact"] >= 0.5 and any(re.search(rf"\b{re.escape(k)}\b", text) for k in nicks) \
                    and now - n["ts"] < 12 * 3600:
                gr.reasons.append(Reason(f"News: {n['title']}", n["url"] or "ESPN news", n["impact"], n["ts"]))
                gr.newest_news_ts = max(gr.newest_news_ts, n["ts"])
        # venue, indoor, back-to-back from ESPN scoreboards
        if starts_at:
            day = et_date(starts_at)          # ESPN scoreboards are keyed by the US Eastern date
            sb = await self.scoreboard(league, day)
            for e in sb.get("events") or []:
                comp = (e.get("competitions") or [{}])[0]
                names = [((c.get("team") or {}).get("displayName") or "") for c in comp.get("competitors") or []]
                if len(names) == 2 and all(max(team_score(n, t) for t in teams) >= 1.0 for n in names):
                    venue = comp.get("venue") or {}
                    indoor = venue.get("indoor")
                    city = ", ".join(x for x in [(venue.get("address") or {}).get("city"),
                                                 (venue.get("address") or {}).get("state")] if x)
                    if league in OUTDOOR_SPORTS and indoor is False and self.conf.get("weather", True):
                        gr.weather = await self.weather(city, starts_at)
                        for fl in gr.weather.get("flags", []):
                            gr.flags.append(f"weather: {fl}")
                            gr.reasons.append(Reason(f"Weather at {venue.get('fullName', city)}: {fl}",
                                                     "Open-Meteo", 0.0, now))
                    break
            if league in ("nba", "nhl"):
                prev = await self.scoreboard(league, day - timedelta(days=1))
                played = {((c.get("team") or {}).get("displayName") or "")
                          for e in prev.get("events") or [] for c in ((e.get("competitions") or [{}])[0].get("competitors") or [])}
                for t in teams:
                    if any(team_score(p, t) >= 1.0 for p in played):
                        gr.flags.append(f"{t.split()[-1]} on a back-to-back")
                        gr.reasons.append(Reason(f"{t} played yesterday (back-to-back)", "ESPN scoreboard", -0.1, now))
        gr.reasons.sort(key=lambda r: -abs(r.value))
        return gr
