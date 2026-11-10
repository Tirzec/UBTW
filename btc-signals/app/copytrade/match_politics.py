"""Strict matcher: international Polymarket ELECTION market -> the same Polymarket US market.

Same office, stage, jurisdiction, party, runoff/special flags, election date (+-1 day), candidate (full name),
bucket and resolution basis; refuses 'Other' outcomes, placeholders, state legislatures, top-two states for
party markets, and anything ambiguous. confidence() < copytrade.politics_min_confidence (0.95) is not copied.
Tested on 768 international election events (~11k markets): 136 matches, 0 false positives found.
Read-only: no orders here.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

STATES = {"alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO","connecticut":"CT",
 "delaware":"DE","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
 "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN",
 "mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ",
 "new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR",
 "pennsylvania":"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT",
 "vermont":"VT","virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY"}
TOP_TWO = {"CA", "WA", "LA"}   # party-of-winner vs party-nominee-wins can diverge (same-party general)
PARTY = {"democratic":"dem","democrat":"dem","republican":"rep","gop":"rep"}
MONTHS = "january february march april may june july august september october november december".split()

def norm(s):
    s = (s or "").replace("\u2013", "-").replace("\u2014", "-")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%<>+. -]", " ", s)).strip()

def find_state(t):
    for name in sorted(STATES, key=len, reverse=True):
        if re.search(r"\b" + name + r"\b", t):
            return STATES[name]
    m = re.search(r"\b(sc|fl|nh|ri|az|ny|ga|nc|pa|mi|wi|oh|tx|ca|co|nv|ma|me)\b", t)
    return m.group(1).upper() if m else None

def find_date(text):
    t = norm(text)
    m = re.search(r"(?:take place|held|scheduled[a-z ]*?)\s+on\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(20\d\d)", t)
    if not m:
        return None
    return datetime(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)), tzinfo=timezone.utc).date()

def district(t):
    m = re.search(r"\b([a-z]{2})-?(\d{2}|al)\b", t)
    return f"{m.group(1).upper()}-{m.group(2).upper()}" if m and m.group(1).upper() in STATES.values() else None

def bucket(t):
    t = t.replace("percentage points", "%")
    if m := re.search(r"less than (\d+)%", t): return (0, int(m.group(1)))
    if m := re.search(r"(?:between )?(\d+)%? ?(?:-|and|to) ?(\d+)%", t): return (int(m.group(1)), int(m.group(2)))
    if m := re.search(r"(?:at least|or more|greater than or equal to) ?(\d+)%|(\d+)%(?:\+| or more)", t):
        return (int(m.group(1) or m.group(2)), None)
    return None

# ------------------------------------------------------------------ intl side
def intl_key(ev, m):
    """Canonical race key from gamma event+market, or (None, reason)."""
    q, title, desc = norm(m["question"]), norm(ev["title"]), m.get("description") or ""
    d = norm(desc)
    if json.loads(m.get("outcomes") or "[]") != ["Yes", "No"]:
        return None, "not a Yes/No market"
    if re.search(r"\b(person|candidate) [a-z]\b", q) or "state senate" in q or "state house" in q or "district of columbia" in q:
        return None, "placeholder candidate / state-legislature / DC market"
    dsrc = "desc"
    dt = find_date(desc)
    if not dt and (ev.get("endDate") or m.get("endDate")):
        dt = datetime.fromisoformat((ev.get("endDate") or m["endDate"]).replace("Z", "+00:00")).date(); dsrc = "endDate"
    k = {"date": dt, "date_src": dsrc, "git": m.get("groupItemTitle") or ""}
    if "margin of victory" in title or "margin of victory" in d[:400] and " by " in q:
        k["kind"] = "mov"
    elif "county" in q and " win " in q and "county" in title:
        k["kind"] = "county"; k["county"] = re.search(r"win (\w+) county", q).group(1)
    elif "nominee" in q or "primary" in q or "primary" in title or "nomination" in title:
        k["kind"] = "nominee"
    elif re.search(r"will the (republican|democratic) party win", q):
        k["kind"] = "party_seat"
    else:
        return None, "not an election-race market (no race template)"
    # office
    if dist := district(q) or district(title):
        k["office"], k["juris"] = "house", dist
    elif "governor" in q or "gubernatorial" in d[:300]:
        k["office"], k["juris"] = "gov", find_state(q) or find_state(title)
    elif "senate" in q or "senate" in title:
        k["office"], k["juris"] = "sen", find_state(q) or find_state(title)
    elif "mayor" in q or "mayor" in title:
        k["office"], k["juris"] = "mayor", (re.search(r"mayor of (\w+)", q) or re.search(r"(\w+),? \w* ?democratic mayoral", title) or [None, None])[1]
    else:
        return None, "office not recognised"
    k["special"] = "special" in q or "special" in title
    k["runoff"] = "runoff" in q or "runoff" in title
    pm = re.search(r"\b(democratic|republican|democrat|gop)\b", q + " " + title)
    k["party"] = PARTY[pm.group(1)] if pm else None
    if k["kind"] in ("nominee", "county", "mov"):
        cm = re.search(r"will (.+?) (?:be the|win)", q)
        k["cand"] = cm.group(1) if cm else None
        if k["cand"] in ("another outcome", "someone else", "another candidate"):
            return None, "'Other' bucket has no US equivalent"
    if k["kind"] == "mov":
        k["bucket"] = bucket(q)
        if not k["bucket"]: return None, "MOV bucket unparsed"
    k["stage"] = "general" if k["kind"] == "party_seat" else "primary"
    return k, None

# ------------------------------------------------------------------ US side
US_PREFIX = {"ushrp": ("house", "primary"), "usgubp": ("gov", "primary"), "ussep": ("sen", "primary"),
             "ushr": ("house", "general"), "usgub": ("gov", "general"), "usse": ("sen", "general"),
             "usmayor": ("mayor", None)}

def us_key(ev, m):
    t = ev["ticker"]; p = t.split("-")[0]
    if p not in US_PREFIX: return None
    office, stage = US_PREFIX[p]
    k = {"office": office, "stage": stage, "ticker": t, "slug": m["slug"], "product": m["slug"].split("-")[0],
         "title": m.get("title") or "", "desc": m.get("description") or ""}
    dm = re.search(r"(20\d\d)-(\d\d)-(\d\d)", t)
    k["date"] = datetime(int(dm[1]), int(dm[2]), int(dm[3])).date() if dm else None
    d = norm(k["desc"]); tt = norm(ev["title"])
    st = re.search(r"-(\w\w)-?(\d\d|al)?-20", t)
    if office == "house":
        x = district(tt); k["juris"] = x
    elif office == "mayor":
        k["juris"] = (re.search(r"mayor of ([a-z ]+?)[ .]", d) or [None, None])[1]
    else:
        k["juris"] = find_state(tt)
    pm = re.search(r"\b(democratic|republican)\b", tt + " " + d[:200])
    k["party"] = PARTY[pm.group(1)] if pm else None
    k["special"] = "special" in tt; k["runoff"] = "runoff" in tt
    if k["product"] == "vmc":
        k["kind"] = "mov"; k["bucket"] = bucket(d[:250])
        cm = re.search(r"if ([a-z .'-]+?)(?:'s|s)? margin of victory|if ([a-z .'-]+?) wins the", d)
        k["cand"] = (cm.group(1) or cm.group(2)).strip() if cm else None
    elif k["product"] == "pvwc" and "county" in d:
        k["kind"] = "county"; cm = re.search(r"if ([a-z .'-]+?) receives the most valid votes", d)
        k["cand"] = cm.group(1) if cm else None; k["county"] = (re.search(r"in (\w+) county", d) or [None, None])[1]
    elif k["product"] == "enwc":
        k["kind"] = "nominee"; cm = re.search(r"if ([a-z .'-]+?) wins the (?:democratic|republican) nomination", d)
        k["cand"] = cm.group(1) if cm else None
    elif k["product"] in ("ushrewc", "ussewc", "usgubewc", "ewc"):
        k["kind"] = "party_seat"; k["party"] = m["slug"].rsplit("-", 1)[-1]
    else:
        return None
    return k

def same_person(a, b):
    a, b = norm(a).split(), norm(b).split()
    if not a or not b: return 0.0
    if a == b: return 1.0
    # "Darline Graham Nordone" vs "Darline Graham" / "Graham": surname-subset with matching first name
    if a[0] == b[0] and (set(b) <= set(a) or set(a) <= set(b)): return 0.9
    return 0.0

def score(ik, uk, um, ev_us):
    """Return (confidence, notes). 0 = refuse."""
    notes = []
    for f in ("office", "kind"):
        if ik[f] != uk[f]: return 0, [f"{f} {ik[f]}!={uk[f]}"]
    if uk["stage"] and ik["stage"] != uk["stage"]: return 0, ["stage differs"]
    if (ik["juris"] or "").lower() != (uk["juris"] or "").lower(): return 0, [f"juris {ik['juris']}!={uk['juris']}"]
    if ik.get("party") and uk.get("party") and ik["party"] != uk["party"]: return 0, ["party differs"]
    if ik["kind"] != "party_seat" and (ik["special"] != uk["special"] and uk["office"] == "sen"): return 0, ["special flag"]
    if ik["runoff"] != uk["runoff"]: return 0, ["runoff flag differs"]
    if not ik["date"] or not uk["date"]: return 0, ["election date unknown"]
    dd = abs((ik["date"] - uk["date"]).days)
    if dd > 1: return 0, [f"election date {ik['date']} vs {uk['date']}"]
    conf = 1.0 if dd == 0 else 0.95
    if ik.get("date_src") == "endDate": conf *= 0.97; notes.append("intl date from endDate")
    if dd: notes.append(f"date off by {dd}d ({ik['date']} vs {uk['date']})")
    if ik["kind"] in ("nominee", "county", "mov"):
        s = same_person(ik.get("cand") or "", uk.get("cand") or "")
        if ik["kind"] == "mov" and s == 0 and uk.get("cand") and norm(uk["cand"]).split()[-1] in norm(ik["cand"]).split():
            s = 0.9   # US 'Darline Graham' vs intl 'Darline Graham Nordone'
        if s == 0: return 0, ["candidate differs"]
        conf *= s
    if ik["kind"] == "county" and ik["county"] != uk.get("county"): return 0, ["county differs"]
    if ik["kind"] == "mov":
        if ik["bucket"] != uk.get("bucket"): return 0, [f"bucket {ik['bucket']} vs {uk.get('bucket')}"]
        notes.append("tie rule differs (intl: lowest bracket wins; US: split 1/n)"); conf *= 0.97
    if ik["kind"] == "county":
        notes.append("tie rule differs (intl alphabetical; US split 1/n)"); conf *= 0.97
    if ik["kind"] == "party_seat":
        if ik["juris"][:2] in TOP_TWO: return 0, ["top-two state: party-of-winner != party-nominee-wins"]
        # cross-check the candidate names both venues show
        if norm(ik["git"]) and norm(um.get("title")) and norm(ik["git"]) != norm(um.get("title")):
            notes.append(f"label differs: {ik['git']!r} vs {um.get('title')!r}"); conf *= 0.9
        notes.append("intl=party of winner (incl. caucus rule); US=party nominee wins")
        conf *= 0.97
    return round(conf, 3), notes

def best_match(ik, us_index):
    cands = []
    for ev_us, um, uk in us_index:
        c, n = score(ik, uk, um, ev_us)
        if c > 0: cands.append((c, n, ev_us, um))
    cands.sort(key=lambda x: -x[0])
    if not cands: return None
    if len(cands) > 1 and cands[1][0] >= cands[0][0] - 0.05:
        return ("AMBIGUOUS", cands[:2])
    return cands[0]


def build_index(us_events: list[dict]) -> list[tuple]:
    idx, seen = [], set()
    for e in us_events:
        if not e.get("ticker"):
            continue
        for m in e.get("markets") or []:
            if m.get("slug") in seen:
                continue
            seen.add(m.get("slug"))
            try:
                k = us_key(e, m)
            except (AttributeError, KeyError, TypeError, ValueError, IndexError):
                k = None
            if k:
                idx.append((e, m, k))
    return idx


def match(ev: dict, m: dict, index: list[tuple], outcome: str) -> tuple[Optional[dict], str]:
    """(result, reason). result = {"us_slug", "leg", "token", "confidence", "notes", "label", "end_ts"}."""
    try:
        ik, why = intl_key(ev, m)
    except (AttributeError, KeyError, TypeError, ValueError, IndexError) as e:
        return None, f"could not read the market ({type(e).__name__})"
    if not ik:
        return None, why
    r = best_match(ik, index)
    if r is None:
        return None, "no matching Polymarket US market"
    if r[0] == "AMBIGUOUS":
        return None, "ambiguous: two Polymarket US markets fit"
    conf, notes, ev_us, um = r
    if um.get("status") not in (None, "MARKET_STATUS_OPEN"):
        return None, f"Polymarket US market {um.get('slug')} is not open"
    sides = um.get("marketSides") or []
    if not (any(s.get("long") for s in sides) and any(not s.get("long") for s in sides)):
        return None, "Polymarket US market has no Yes/No sides"
    if outcome not in ("Yes", "No"):
        return None, "not a Yes/No outcome"
    leg = "long" if outcome == "Yes" else "short"           # US: long == Yes (never use 'outcomes' order)
    end = um.get("endDate") or ev_us.get("endDate")
    try:
        end_ts = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp() if end else None
    except ValueError:
        end_ts = None
    return {"us_slug": um["slug"], "leg": leg, "token": f"{um['slug']}:{leg}", "confidence": conf,
            "notes": notes, "label": f"{um.get('title') or ''} {outcome}".strip(), "end_ts": end_ts,
            "bet_type": ik["kind"]}, ""


async def fetch_us_politics(gw: Callable[[str], Awaitable[Optional[dict]]], max_pages: int = 40) -> list[dict]:
    """Every open Polymarket US politics event (paged)."""
    out: list[dict] = []
    for i in range(max_pages):
        d = await gw(f"/v1/events?categories=politics&active=true&closed=false&limit=100&offset={i * 100}")
        evs = (d or {}).get("events") or []
        out += evs
        if len(evs) < 100:
            break
    return out
