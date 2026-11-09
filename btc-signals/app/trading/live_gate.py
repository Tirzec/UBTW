"""Every check that must pass before LIVE trading can start. Runs in the terminal,
before the server starts, and requires a typed confirmation. Never bypassed."""
from __future__ import annotations

import os
import sqlite3
import sys

import httpx

from .store import SCHEMA, bot_stats

CONFIRM_PHRASE = "I UNDERSTAND THIS TRADES REAL MONEY"


def paper_record(db_path: str) -> tuple[int, dict]:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    rows = [dict(r) for r in c.execute("SELECT * FROM bot_positions WHERE mode='paper' AND "
                                       "status='closed' ORDER BY closed_ts")]
    c.close()
    return len(rows), bot_stats(rows)


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
                     input_fn=input, geoblock_fn=geoblock_status) -> dict:
    """Return credentials if LIVE may start; otherwise raise SystemExit explaining every failure."""
    tc = cfg.get("trading", {}) or {}
    need = int(tc.get("min_paper_trades_for_live", 300))
    n, stats = paper_record(db_path)
    problems: list[str] = []
    if not tc.get("live_trading_enabled", False):
        problems.append("trading.live_trading_enabled is not true in config.yaml")
    if n < need:
        problems.append(f"only {n} paper trades completed; {need} required before live trading")
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
