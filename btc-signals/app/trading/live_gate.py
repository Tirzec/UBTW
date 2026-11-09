"""Every check that must pass before LIVE trading can start. Runs in the terminal,
before the server starts, and requires a typed confirmation. Never bypassed."""
from __future__ import annotations

import os
import sqlite3
import sys

import httpx

from .store import SCHEMA, bot_stats

CONFIRM_PHRASE = "I UNDERSTAND THIS TRADES REAL MONEY"


def paper_record(db_path: str, venue: str = "polymarket_intl",
                 module: str = "crypto_updown", since: float = 0.0) -> tuple[int, dict]:
    """Closed paper trades ON THIS VENUE and MODULE (practice on one exchange doesn't count for the
    other, and sports trades don't count toward the crypto requirement)."""
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(bot_positions)")}
    if "venue" in cols and "module" in cols:
        rows = c.execute("SELECT * FROM bot_positions WHERE mode='paper' AND status='closed' AND "
                         "COALESCE(venue,'polymarket_intl')=? AND COALESCE(module,'crypto_updown')=? "
                         "ORDER BY closed_ts", (venue, module))
    elif "venue" in cols:
        rows = c.execute("SELECT * FROM bot_positions WHERE mode='paper' AND status='closed' AND "
                         "COALESCE(venue,'polymarket_intl')=? ORDER BY closed_ts", (venue,))
    else:
        rows = c.execute("SELECT * FROM bot_positions WHERE mode='paper' AND status='closed' "
                         "ORDER BY closed_ts") if venue == "polymarket_intl" else []
    rows = [dict(r) for r in rows if (dict(r).get("opened_ts") or 0) >= since]
    c.close()
    return len(rows), bot_stats(rows)


def us_account_check(key_id: str, secret: str, api: str = "https://api.polymarket.us",
                     timeout: float = 10) -> tuple[bool, str]:
    """Signed GET /v1/account/balances: proves the API key works before going live."""
    try:
        from .us_broker import Signer
        path = "/v1/account/balances"
        r = httpx.get(api + path, headers=Signer(key_id, secret).headers("GET", path), timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        bals = r.json().get("balances") or []
        usd = next((b for b in bals if (b.get("currency") or "USD") == "USD"), {})
        return True, f"buying power ${float(usd.get('buyingPower') or 0):,.2f}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def geoblock_status(timeout: float = 10) -> tuple[bool | None, str]:
    """Polymarket's official eligibility check for this IP (docs: /api-reference/geoblock)."""
    try:
        r = httpx.get("https://polymarket.com/api/geoblock", timeout=timeout)
        d = r.json()
        return bool(d.get("blocked")), f"{d.get('country', '?')}-{d.get('region', '?')}"
    except Exception as e:  # noqa: BLE001
        return None, f"check failed: {e}"


def format_stats(n: int, s: dict) -> str:
    if not n:
        return "  no trades yet"
    return (f"  trades {n} | win rate {s['win_rate']:.1%} | total P&L {s['total_pnl']:+.2f} | "
            f"EV/trade {s['ev_per_trade']:+.3f}\n"
            f"  avg win {s['avg_win']:+.3f} vs avg loss {s['avg_loss']:+.3f} | "
            f"max drawdown {s['max_drawdown']:.2f} | fees paid {s['fees_paid']:.2f}\n"
            f"  exits: {s['exit_triggers']}")


def check_live_gates(cfg: dict, db_path: str, *, interactive: bool = True,
                     input_fn=input, geoblock_fn=geoblock_status, account_fn=us_account_check) -> dict:
    """Return credentials if LIVE may start; otherwise raise SystemExit explaining every failure."""
    tc = cfg.get("trading", {}) or {}
    venue = (cfg.get("venue") or "polymarket_intl").strip()
    need = int(tc.get("min_paper_trades_for_live", 300))
    from .store import paper_count_from
    n, stats = paper_record(db_path, venue, since=paper_count_from(tc))
    problems: list[str] = []
    if not tc.get("live_trading_enabled", False):
        problems.append("trading.live_trading_enabled is not true in config.yaml")
    if n < need:
        problems.append(f"only {n} paper trades completed on {venue}; {need} required before live trading")
    if venue == "polymarket_us":
        return _check_us(cfg, tc, n, need, stats, problems, interactive, input_fn, account_fn)
    if sys.version_info < (3, 11):
        problems.append(f"Python {sys.version.split()[0]} - the official Polymarket SDK needs 3.11+")
    else:
        try:
            import polymarket  # noqa: F401  # type: ignore
        except ImportError:
            problems.append('official SDK missing: pip install "polymarket-client>=0.12.0"')
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS", "").strip()
    if not key:
        problems.append("POLYMARKET_PRIVATE_KEY not set in .env")
    if not wallet:
        problems.append("POLYMARKET_WALLET_ADDRESS not set in .env")
    blocked, where = geoblock_fn()
    if blocked is None:
        problems.append(f"could not verify location with Polymarket ({where})")
    elif blocked:
        problems.append(f"Polymarket blocks order placement from your location ({where}). "
                        "Live trading is not available here; do not use a VPN to get around it")
    print("\n=== LIVE TRADING CHECK ===")
    print(f"Paper record ({n}/{need} trades):\n{format_stats(n, stats)}")
    if problems:
        raise SystemExit("\nLIVE mode refused:\n  - " + "\n  - ".join(problems) +
                         "\n\nStart in paper mode instead (trading.mode: paper).")
    rc = tc.get("risk", {}) or {}
    print(f"\nRisk limits: max stake ${rc.get('max_stake_usd', 5)}, daily loss limit "
          f"${rc.get('daily_loss_limit_usd', 25)}, max {rc.get('max_consecutive_losses', 5)} losses in a "
          f"row, max exposure ${rc.get('max_total_exposure_usd', 15)}")
    print("Use a separate wallet funded only with money you can afford to lose.")
    if not interactive or not sys.stdin.isatty() and input_fn is input:
        raise SystemExit("LIVE mode needs a typed confirmation in an interactive terminal.")
    typed = input_fn(f'\nType exactly "{CONFIRM_PHRASE}" to trade real money: ').strip()
    if typed != CONFIRM_PHRASE:
        raise SystemExit("Confirmation did not match. Not starting.")
    return {"private_key": key, "wallet": wallet}


def _confirm(interactive: bool, input_fn) -> None:
    if not interactive or not sys.stdin.isatty() and input_fn is input:
        raise SystemExit("LIVE mode needs a typed confirmation in an interactive terminal.")
    typed = input_fn(f'\nType exactly "{CONFIRM_PHRASE}" to trade real money: ').strip()
    if typed != CONFIRM_PHRASE:
        raise SystemExit("Confirmation did not match. Not starting.")


def _check_us(cfg, tc, n, need, stats, problems, interactive, input_fn, account_fn) -> dict:
    """Polymarket US (CFTC-regulated, for US residents): API key from polymarket.us/developer."""
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    try:
        import nacl  # noqa: F401
    except ImportError:
        problems.append("PyNaCl missing: pip install -r requirements.txt")
    from .live_phase import phase_of
    if phase_of(cfg) != 1:
        problems.append(f"trading.live_phase is {phase_of(cfg)}; only phase 1 exists (set live_phase: 1)")
    if not key_id:
        problems.append("POLYMARKET_KEY_ID not set in .env (create a key at polymarket.us/developer)")
    if not secret:
        problems.append("POLYMARKET_SECRET_KEY not set in .env")
    account = ""
    if key_id and secret and not problems:
        ok, account = account_fn(key_id, secret, (cfg.get("polymarket_us", {}) or {}).get(
            "api_url", "https://api.polymarket.us"))
        if not ok:
            problems.append(f"Polymarket US API key check failed: {account}")
    print("\n=== LIVE TRADING CHECK: Polymarket US ===")
    print(f"Paper record on Polymarket US ({n}/{need} trades):\n{format_stats(n, stats)}")
    if problems:
        raise SystemExit("\nLIVE mode refused:\n  - " + "\n  - ".join(problems) +
                         "\n\nKeep running in paper mode (trading.mode: paper).")
    rc = tc.get("risk", {}) or {}
    from .live_phase import PHASE1_MAX_DAILY_LOSS_USD, PHASE1_MAX_STAKE_USD, PHASE1_MAX_TRADES_PER_DAY
    print(f"\nAccount: {account}")
    print(f"LIVE PHASE 1 hard limits (config can only lower them): stake <= ${PHASE1_MAX_STAKE_USD:g}, "
          f"daily loss <= ${PHASE1_MAX_DAILY_LOSS_USD:g}, <= {PHASE1_MAX_TRADES_PER_DAY} live trades/day. "
          "Positions are reconciled with the exchange every 60 s; any mismatch halts trading.")
    print(f"Risk limits: max stake ${rc.get('max_stake_usd', 5)}, daily loss limit "
          f"${rc.get('daily_loss_limit_usd', 25)}, max {rc.get('max_consecutive_losses', 5)} losses in a "
          f"row, max exposure ${rc.get('max_total_exposure_usd', 15)}")
    print("Taker fee on Polymarket US: 0.095 x p x (1-p) per contract (0.0695 in your first 7 days).")
    print("Only trade money you can afford to lose. You must be 18+ (19 in AL/NE, 21 in MS) and in an "
          "eligible state.")
    _confirm(interactive, input_fn)
    return {"key_id": key_id, "secret_key": secret}
