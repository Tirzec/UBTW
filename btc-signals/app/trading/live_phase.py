"""First live phase: hard limits that config can LOWER but never raise.

While `trading.live_phase` is 1 (the only phase that exists so far), LIVE trading is capped at:
  - stake per trade         <= $1 (and config's own max_stake_usd if lower)
  - daily loss, all modules <= $10 (and config's own limits if lower)
  - live entries per day    <= 10, across crypto + sports, unless live_phase1.max_trades_per_day is
                               set to off (user's choice 2026-11-10); it can never be raised above 10
These numbers are in code on purpose: raising them means changing this file, not a setting.
"""
from __future__ import annotations

import time

from .risk import OFF

PHASE1_MAX_STAKE_USD = 1.0
PHASE1_MAX_DAILY_LOSS_USD = 10.0
PHASE1_MAX_TRADES_PER_DAY = 10


def phase_of(cfg: dict) -> int:
    return int(((cfg.get("trading", {}) or {}).get("live_phase", 1)) or 1)


def apply_phase1(cfg: dict) -> dict:
    """Clamp every live risk limit in cfg to phase-1 values (in place). Returns what is in force."""
    tc = cfg.setdefault("trading", {})
    sc = cfg.setdefault("sports", {})
    rg = cfg.setdefault("risk_global", {})
    for conf in (tc.setdefault("risk", {}), sc.setdefault("risk", {})):
        conf["max_stake_usd"] = min(float(conf.get("max_stake_usd", PHASE1_MAX_STAKE_USD)), PHASE1_MAX_STAKE_USD)
        if conf.get("daily_loss_limit_usd", PHASE1_MAX_DAILY_LOSS_USD) in OFF:
            conf["daily_loss_limit_usd"] = "off"        # switched off by the user
        else:
            conf["daily_loss_limit_usd"] = min(float(conf.get("daily_loss_limit_usd", PHASE1_MAX_DAILY_LOSS_USD)),
                                               PHASE1_MAX_DAILY_LOSS_USD)
    for entry in (tc.setdefault("entry", {}), sc.setdefault("entry", {})):
        entry["stake_usd"] = min(float(entry.get("stake_usd", PHASE1_MAX_STAKE_USD)), PHASE1_MAX_STAKE_USD)
    if rg.get("daily_loss_limit_usd", PHASE1_MAX_DAILY_LOSS_USD) in OFF:
        rg["daily_loss_limit_usd"] = "off"
    else:
        rg["daily_loss_limit_usd"] = min(float(rg.get("daily_loss_limit_usd", PHASE1_MAX_DAILY_LOSS_USD)),
                                         PHASE1_MAX_DAILY_LOSS_USD)
    wanted = (tc.get("live_phase1", {}) or {}).get("max_trades_per_day", PHASE1_MAX_TRADES_PER_DAY)
    if wanted in (None, False, "off", "none"):
        rg["max_trades_per_day"] = None                    # switched off by the user
    else:
        rg["max_trades_per_day"] = max(0, min(int(wanted), PHASE1_MAX_TRADES_PER_DAY))
    return {"phase": 1, "max_stake_usd": tc["risk"]["max_stake_usd"],
            "daily_loss_limit_usd": rg["daily_loss_limit_usd"],
            "max_trades_per_day": rg["max_trades_per_day"]}


def live_entries_today(db, now: float | None = None, venue: str = "polymarket_us") -> int:
    """LIVE entries opened since local midnight, every module (test orders excluded)."""
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    r = db.query("SELECT COUNT(*) n FROM bot_positions WHERE mode='live' AND opened_ts>=? AND status!='void' "
                 "AND COALESCE(venue,'polymarket_intl')=? AND COALESCE(module,'crypto_updown')!='test_order'",
                 (midnight, venue))
    return int(r[0]["n"])
