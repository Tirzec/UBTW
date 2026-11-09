"""Live-readiness PREFLIGHT: read-only PASS / FAIL checks. Places, changes and cancels nothing.

    python -m app.preflight                       # all checks
    python -m app.preflight --expect-balance 50   # also require the API balance to equal the app's
    python -m app.preflight --skip-feeds          # skip the ~20 s price-feed test

Every request to your account is a signed GET. Exit code 0 only if nothing FAILed.
A report (no secrets) is saved to data/preflight-<time>.json.

Status meanings: PASS ok | FAIL must be fixed before live | WARN works but look at it |
CHECK you must confirm by eye | INFO for your information.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field

import httpx

from .config import ROOT, load_config, resolve_path

GATEWAY = "https://gateway.polymarket.us"
API = "https://api.polymarket.us"


@dataclass
class Check:
    name: str
    status: str            # PASS | FAIL | WARN | CHECK | INFO
    detail: str
    lines: list = field(default_factory=list)


# ----------------------------------------------------------------------------- helpers
def _amt(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _first_list(d, *keys):
    """The list under one of `keys`, else the first list value (response shapes vary)."""
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for k in keys:
        v = d.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):                          # e.g. positions keyed by market slug
            return [dict(val, _key=k2) if isinstance(val, dict) else {"_key": k2, "value": val}
                    for k2, val in v.items()]
    return next((v for v in d.values() if isinstance(v, list)), [])


def parse_book(data: dict) -> tuple[list, list]:
    md = data.get("marketData") or data
    bids = [(_amt(x.get("px")), float(x.get("qty") or 0)) for x in md.get("bids") or []]
    asks = [(_amt(x.get("px")), float(x.get("qty") or 0)) for x in md.get("offers") or []]
    bids = sorted([(p, q) for p, q in bids if p is not None and q > 0], reverse=True)
    asks = sorted([(p, q) for p, q in asks if p is not None and q > 0])
    return bids, asks


def book_summary(bids, asks) -> str:
    if not bids and not asks:
        return "book empty"
    b = f"bid {bids[0][0]:.4g} x {bids[0][1]:,.0f}" if bids else "no bids"
    a = f"ask {asks[0][0]:.4g} x {asks[0][1]:,.0f}" if asks else "no asks"
    return f"{b} / {a}"


class Account:
    """Signed, GET-only client. There is deliberately no method that can send anything else."""

    def __init__(self, key_id: str, secret: str, base: str = API, client: httpx.Client | None = None):
        from .trading.us_broker import Signer
        self.signer = Signer(key_id, secret)
        self.base = base
        self.client = client or httpx.Client(timeout=10)

    def get(self, path: str) -> httpx.Response:
        return self.client.get(self.base + path, headers=self.signer.headers("GET", path))


# ----------------------------------------------------------------------------- checks
def check_secrets() -> list[Check]:
    from .env_guard import check_env_safety
    out = []
    try:
        warns = check_env_safety()
        out.append(Check("secrets not tracked by git", "PASS", ".env and key files are not in git"))
        for w in warns:
            out.append(Check("secrets file name", "WARN", w))
    except SystemExit as e:
        out.append(Check("secrets not tracked by git", "FAIL", str(e).strip().splitlines()[0],
                         str(e).strip().splitlines()[1:]))
    env = ROOT / ".env"
    out.append(Check(".env file", "PASS" if env.exists() else "FAIL",
                     str(env) if env.exists() else "no .env in the btc-signals folder"))
    return out


def check_config(cfg: dict) -> list[Check]:
    tc = cfg.get("trading", {}) or {}
    sc = cfg.get("sports", {}) or {}
    venue = (cfg.get("venue") or "polymarket_intl").strip()
    out = [Check("venue", "PASS" if venue == "polymarket_us" else "FAIL",
                 venue if venue == "polymarket_us" else f"venue is {venue}; must be polymarket_us")]
    out.append(Check("current mode", "INFO",
                     f"trading.mode={tc.get('mode', 'paper')}, live_trading_enabled="
                     f"{bool(tc.get('live_trading_enabled'))}, sports.live_enabled={bool(sc.get('live_enabled'))}"
                     " (preflight never changes these)"))
    return out


def check_clock_(ntp_timeout: float = 2.0) -> list[Check]:
    from .clock import check_clock
    r = check_clock(ntp_timeout=ntp_timeout)
    out = [Check("clock sync", r.status, r.detail)]
    wt = r.windows_time
    if wt:
        if wt.get("service_running"):
            out.append(Check("Windows Time service", "PASS",
                             f"source {wt.get('Source', '?')}, last sync {wt.get('Last Successful Sync Time', '?')}"))
        else:
            out.append(Check("Windows Time service", "WARN",
                             "not running: the clock can drift. Fix: python -m app.clock"))
    return out


def check_account(cfg: dict, expect_balance: float | None, account: Account | None = None) -> list[Check]:
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not key_id or not secret:
        missing = [n for n, v in (("POLYMARKET_KEY_ID", key_id), ("POLYMARKET_SECRET_KEY", secret)) if not v]
        return [Check("API authentication", "FAIL", f"{', '.join(missing)} not set in .env")]
    api = (cfg.get("polymarket_us", {}) or {}).get("api_url", API)
    try:
        acct = account or Account(key_id, secret, api)
    except Exception as e:  # noqa: BLE001 - e.g. secret isn't valid base64
        return [Check("API authentication", "FAIL", f"secret key unusable ({type(e).__name__}); "
                      "re-copy it from polymarket.us/developer")]
    out: list[Check] = []
    try:
        r = acct.get("/v1/account/balances")
    except httpx.HTTPError as e:
        return [Check("API authentication", "FAIL", f"network error: {type(e).__name__}: {e}")]
    if r.status_code in (401, 403):
        hint = "key ID / secret wrong, key deleted, or the PC clock is off by >30 s"
        return [Check("API authentication", "FAIL", f"HTTP {r.status_code}: {hint}")]
    if r.status_code != 200:
        return [Check("API authentication", "FAIL", f"HTTP {r.status_code}: {r.text[:150]}")]
    out.append(Check("API authentication", "PASS", "signed request accepted"))

    bals = _first_list(r.json(), "balances")
    usd = next((b for b in bals if (b.get("currency") or "USD") == "USD"), None)
    if usd is None:
        out.append(Check("balance", "FAIL", "no USD balance in the response"))
    else:
        bp = _amt(usd.get("buyingPower"))
        cur = _amt(usd.get("currentBalance"))
        shown = f"buying power ${bp:,.2f}" if bp is not None else "buying power ?"
        if cur is not None:
            shown += f", balance ${cur:,.2f}"
        min_bal = float(((cfg.get("trading", {}) or {}).get("risk", {}) or {}).get("min_wallet_balance_usd", 10))
        if expect_balance is not None:
            match = any(v is not None and abs(v - expect_balance) < 0.005 for v in (bp, cur))
            out.append(Check("balance matches app", "PASS" if match else "FAIL",
                             f"{shown}; you said the app shows ${expect_balance:,.2f}"))
        else:
            out.append(Check("balance matches app", "CHECK",
                             f"{shown}. Does the Polymarket US app show the same? Re-run with "
                             "--expect-balance <amount> to make this a PASS/FAIL check"))
        if bp is not None and bp < min_bal:
            out.append(Check("balance above minimum", "FAIL",
                             f"buying power ${bp:,.2f} < risk.min_wallet_balance_usd ${min_bal:g}"))
        elif bp is not None:
            out.append(Check("balance above minimum", "PASS", f"${bp:,.2f} >= ${min_bal:g}"))

    for path, name, keys in (("/v1/orders/open", "open orders", ("orders", "openOrders")),
                             ("/v1/portfolio/positions", "open positions", ("positions", "marketPositions"))):
        try:
            rr = acct.get(path)
            if rr.status_code != 200:
                out.append(Check(name, "FAIL", f"HTTP {rr.status_code}: {rr.text[:120]}"))
                continue
            items = _first_list(rr.json(), *keys)
        except (httpx.HTTPError, ValueError) as e:
            out.append(Check(name, "FAIL", f"{type(e).__name__}: {e}"))
            continue
        lines = []
        for it in items[:10]:
            slug = it.get("marketSlug") or it.get("slug") or it.get("_key") or "?"
            qty = it.get("quantity", it.get("netPosition", it.get("qty", "?")))
            lines.append(f"{slug}: {it.get('intent') or it.get('side') or ''} qty {qty} "
                         f"{it.get('state', '')}".strip())
        if name == "open orders":
            st = "PASS" if not items else "WARN"
            det = "none" if not items else (f"{len(items)} open. NOTE: when the bot starts in live mode it "
                                            "cancels ALL open orders on the account, including ones you placed")
        else:
            st, det = "INFO", ("none" if not items else f"{len(items)} position(s); the bot leaves positions "
                               "it didn't open alone")
        out.append(Check(name, st, det, lines))
    return out


def _book(client: httpx.Client, gateway: str, slug: str):
    r = client.get(f"{gateway}/v1/markets/{slug}/book")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return parse_book(r.json())


def check_crypto_markets(cfg: dict, client: httpx.Client, now: float | None = None) -> list[Check]:
    from .ingest.discovery import DURATION_SECONDS, Window
    from .ingest.us_markets import apply_us_market, us_slug
    uc = cfg.get("polymarket_us", {}) or {}
    gw = uc.get("gateway_url", GATEWAY)
    now = now or time.time()
    out = []
    for dur in uc.get("durations", ["5m", "15m"]):
        sec = DURATION_SECONDS[dur]
        start = int(now // sec * sec)
        slug = us_slug(dur, start)
        name = f"crypto BTC {dur} market"
        try:
            r = client.get(f"{gw}/v1/market/slug/{slug}")
            if r.status_code == 404:
                out.append(Check(name, "FAIL", f"{slug} not found"))
                continue
            r.raise_for_status()
            m = r.json().get("market") or {}
            w = apply_us_market(Window(dur, start, start + sec, slug, venue="polymarket_us"), m)
            problems = []
            if not w.accepting_orders:
                problems.append(f"status {m.get('status')}")
            if w.rules is not None and not w.rules.ok:
                problems.append("rules mismatch: " + "; ".join(w.rules.mismatches)[:150])
            spec = (f"tick {w.tick_size:g}, min size {w.min_size:g}, step {w.size_step:g}, "
                    f"taker fee coefficient {w.fee_schedule.get('rate')}")
            out.append(Check(name, "FAIL" if problems else "PASS",
                             f"{slug}: " + ("; ".join(problems) if problems else spec)))
            bk = _book(client, gw, slug)
            bids, asks = bk or ([], [])
            left = start + sec - now
            if bids and asks:
                out.append(Check(f"crypto BTC {dur} order book", "PASS", book_summary(bids, asks)))
            else:
                out.append(Check(f"crypto BTC {dur} order book", "WARN",
                                 f"{book_summary(bids, asks)} ({int(left)} s left in the window; new books "
                                 "can take ~2 min to fill - re-run if this persists)"))
        except (httpx.HTTPError, ValueError) as e:
            out.append(Check(name, "FAIL", f"{type(e).__name__}: {e}"))
    return out


def check_sports_markets(cfg: dict, client: httpx.Client, now: float | None = None,
                         max_events: int = 2) -> list[Check]:
    from .modules.sports.markets import _ts, liquidity_ok, parse_event
    sc = cfg.get("sports", {}) or {}
    if not sc.get("enabled", False):
        return [Check("sports markets", "INFO", "sports module disabled")]
    now = now or time.time()
    leagues = [k for k, v in (sc.get("leagues") or {}).items() if (v or {}).get("enabled")]
    liq = sc.get("liquidity", {}) or {}
    lines, any_market, any_book = [], False, False
    for lg in leagues:
        try:
            r = client.get(f"{GATEWAY}/v2/leagues/{lg}/events", params={"limit": 100})
            if r.status_code == 404:
                lines.append(f"{lg.upper()}: league not found")
                continue
            r.raise_for_status()
            evs = [e for e in r.json().get("events") or []
                   if (_ts(e.get("startDate") or e.get("startTime")) or 0) > now]
            evs.sort(key=lambda e: _ts(e.get("startDate") or e.get("startTime")) or 0)
            if not evs:
                lines.append(f"{lg.upper()}: no upcoming games listed")
                continue
            note = f"{lg.upper()}: {len(evs)} upcoming games"
            for ev in evs[:max_events]:
                time.sleep(0.15)                                 # stay far under 25 req/s
                full = client.get(f"{GATEWAY}/v1/events/slug/{ev['slug']}")
                if full.status_code != 200:
                    continue
                g = parse_event(full.json().get("event") or {}, lg)
                ml = [m for m in g.markets.values() if m.bet_type == "moneyline"]
                if not ml:
                    continue
                any_market = True
                m = ml[0]
                bids, asks = _book(client, GATEWAY, m.slug) or ([], [])
                ok, why = liquidity_ok({"bids": bids, "asks": asks}, float(liq.get("max_spread", 0.03)),
                                       float(liq.get("min_depth", 200)))
                any_book = any_book or bool(bids and asks)
                note += (f"; {g.title or m.slug}: moneyline tick {m.tick_size:g}, "
                         f"rules {'ok' if m.rules_ok else 'NOT ok: ' + '; '.join(m.rules_issues)[:80]}, "
                         f"{book_summary(bids, asks)}, liquidity filter {'ok' if ok else why}")
                break
            lines.append(note)
        except (httpx.HTTPError, ValueError) as e:
            lines.append(f"{lg.upper()}: {type(e).__name__}: {e}"[:160])
    if any_market and any_book:
        st, det = "PASS", "markets and live order books found"
    elif any_market:
        st, det = "WARN", "markets found but no two-sided order book yet (books fill closer to game time)"
    else:
        st, det = "FAIL", "no open sports markets found in any enabled league"
    return [Check("sports markets and books", st, det, lines)]


async def check_feeds(cfg: dict, wait_s: float = 20) -> list[Check]:
    from .feeds.manager import FeedManager
    fm = FeedManager(cfg)
    await fm.start()
    try:
        deadline = time.time() + wait_s
        q = None
        while time.time() < deadline:
            await asyncio.sleep(1)
            q = fm.tick_health()
            if not q.degraded and time.time() > deadline - wait_s + 8:
                break
        health = fm.health()
    finally:
        await fm.stop()
    lines = [f"{h['name']} ({h['role']}): " + ("fresh" if not h["stale"] else f"STALE - {h['stale_reason']}")
             + (f", last tick {h['age_s']}s ago" if h.get("age_s") is not None else "") for h in health]
    ref_ok = any(not h["stale"] for h in health if h["role"] == "reference")
    spot_ok = any(not h["stale"] for h in health if h["role"] == "spot")
    if ref_ok and spot_ok and q is not None and not q.degraded:
        return [Check("price feeds", "PASS", "settlement index + spot feeds fresh, data quality OK", lines)]
    reasons = "; ".join(q.reasons) if q is not None else "no data"
    return [Check("price feeds", "FAIL", f"data quality degraded: {reasons}", lines)]


def check_risk(cfg: dict, buying_power: float | None) -> list[Check]:
    tc = cfg.get("trading", {}) or {}
    sc = cfg.get("sports", {}) or {}
    rg = cfg.get("risk_global", {}) or {}
    out = []
    for label, rc, stake in (("crypto", tc.get("risk", {}) or {}, (tc.get("entry", {}) or {}).get("stake_usd")),
                             ("sports", sc.get("risk", {}) or {}, (sc.get("entry", {}) or {}).get("stake_usd"))):
        p = []
        ms = float(rc.get("max_stake_usd", 0) or 0)
        dl = float(rc.get("daily_loss_limit_usd", 0) or 0)
        ex = float(rc.get("max_total_exposure_usd", 0) or 0)
        cl = int(rc.get("max_consecutive_losses", 0) or 0)
        for n, v in (("max_stake_usd", ms), ("daily_loss_limit_usd", dl), ("max_total_exposure_usd", ex)):
            if not (v > 0 and math.isfinite(v)):
                p.append(f"{n} must be > 0")
        if ms > dl > 0:
            p.append("max stake is larger than the daily loss limit")
        if ms > ex > 0:
            p.append("max stake is larger than max exposure")
        if not 1 <= cl <= 20:
            p.append("max_consecutive_losses should be 1-20")
        if stake is not None and float(stake) > ms > 0:
            p.append(f"entry stake ${stake} > max stake ${ms:g} (it will be capped)")
        out.append(Check(f"risk limits ({label})", "FAIL" if p else "PASS",
                         "; ".join(p) if p else f"stake <= ${ms:g}, daily loss <= ${dl:g}, exposure <= ${ex:g}, "
                                                f"{cl} losses in a row -> pause"))
    gd = float(rg.get("daily_loss_limit_usd", 0) or 0)
    ge = float(rg.get("max_total_exposure_usd", 0) or 0)
    p = [] if gd > 0 and ge > 0 else ["risk_global limits must be > 0"]
    out.append(Check("risk limits (all modules)", "FAIL" if p else "PASS",
                     "; ".join(p) or f"daily loss <= ${gd:g}, exposure <= ${ge:g} across crypto + sports"))
    if buying_power is not None and gd > 0:
        frac = gd / buying_power if buying_power else float("inf")
        st = "PASS" if frac <= 0.5 else "WARN"
        out.append(Check("daily loss limit vs balance", st,
                         f"global daily loss limit ${gd:g} = {frac:.0%} of buying power ${buying_power:,.2f}"
                         + ("" if st == "PASS" else ". The first live phase (step 5) caps it at $10/day")))
    return out


def check_paper(cfg: dict) -> list[Check]:
    from .db import DB
    from .trading.live_gate import format_stats
    from .trading.store import TradeStore, bot_stats
    db = DB(resolve_path(cfg, cfg.get("db_path", "data/signals.db")))
    out = []
    for module, label, need in (
            ("crypto_updown", "crypto", int((cfg.get("trading", {}) or {}).get("min_paper_trades_for_live", 300))),
            ("sports", "sports", int((cfg.get("sports", {}) or {}).get("min_paper_trades_for_live", 200)))):
        rows = TradeStore(db, "paper", "polymarket_us", module).closed_positions()
        n = len(rows)
        out.append(Check(f"paper trades ({label})", "PASS" if n >= need else "FAIL",
                         f"{n} / {need} closed paper trades on Polymarket US",
                         format_stats(n, bot_stats(rows)).splitlines() if n else []))
    return out


# ----------------------------------------------------------------------------- runner
def run(cfg: dict, expect_balance: float | None = None, skip_feeds: bool = False) -> list[Check]:
    checks: list[Check] = []
    checks += check_secrets()
    checks += check_config(cfg)
    checks += check_clock_()
    acct = check_account(cfg, expect_balance)
    checks += acct
    bp = None
    for c in acct:
        if c.name.startswith("balance") and "buying power $" in c.detail:
            try:
                bp = float(c.detail.split("buying power $")[1].split()[0].rstrip(",;.").replace(",", ""))
            except (IndexError, ValueError):
                pass
            break
    with httpx.Client(timeout=10, headers={"User-Agent": "btc-signals-preflight/1.0"}) as client:
        checks += check_crypto_markets(cfg, client)
        checks += check_sports_markets(cfg, client)
    if skip_feeds:
        checks.append(Check("price feeds", "WARN", "skipped (--skip-feeds)"))
    else:
        checks += asyncio.run(check_feeds(cfg))
    checks += check_risk(cfg, bp)
    checks += check_paper(cfg)
    return checks


def print_report(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    print("\n=== PREFLIGHT (read-only: nothing is placed, changed or cancelled) ===\n")
    for c in checks:
        print(f"[{c.status:5s}] {c.name:{width}s}  {c.detail}")
        for ln in c.lines:
            print(f"{'':8s}{'':{width}s}    {ln}")
    n = {s: sum(c.status == s for c in checks) for s in ("PASS", "FAIL", "WARN", "CHECK", "INFO")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['WARN']} warnings, {n['CHECK']} to confirm by eye")
    if n["FAIL"]:
        print("NOT READY for live trading: fix every FAIL first.")
    elif n["CHECK"]:
        print("No failures. Confirm the CHECK items yourself.")
    else:
        print("All checks passed.")
    return 1 if n["FAIL"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.preflight", description=__doc__.split("\n\n")[0])
    ap.add_argument("--expect-balance", type=float, help="the balance the Polymarket US app shows (USD)")
    ap.add_argument("--skip-feeds", action="store_true", help="skip the ~20 s price-feed check")
    a = ap.parse_args(argv)
    from .console import safe_console
    safe_console()
    import logging
    logging.basicConfig(level=logging.ERROR)
    from .trading.store import install_redaction
    install_redaction()
    cfg = load_config()
    print("Running preflight (about 30-40 s) ...", flush=True)
    checks = run(cfg, a.expect_balance, a.skip_feeds)
    code = print_report(checks)
    try:
        out = resolve_path(cfg, f"data/preflight-{time.strftime('%Y%m%d-%H%M%S')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ts": time.time(), "checks": [asdict(c) for c in checks]}, indent=1),
                       encoding="utf-8")
        print(f"Report saved: {out}")
    except OSError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
