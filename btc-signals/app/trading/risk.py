"""Risk controls for the auto-trader. Pure logic over a small state object.

Hard stops (need you to press Resume):   kill switch, consecutive losses, API errors, low balance
Day stop (clears at local midnight):     daily loss limit
Soft blocks (clear by themselves):       degraded data, exposure, one position per window,
                                         matching-engine maintenance
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def next_local_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return (datetime(d.year, d.month, d.day) + timedelta(days=1)).timestamp()


@dataclass
class RiskState:
    halted: bool = False             # kill switch / hard pause; manual resume required
    halt_reason: str = ""
    day: str = ""
    day_pnl: float = 0.0
    day_trades: int = 0
    day_stopped_until: float = 0.0   # daily loss limit hit
    consecutive_losses: int = 0
    api_errors: list[float] = field(default_factory=list)
    maintenance_until: float = 0.0   # matching engine restart / post-only mode

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "api_errors"} | {
            "recent_api_errors": len(self.api_errors)}


class RiskManager:
    def __init__(self, conf: dict, state: RiskState | None = None):
        self.max_stake = float(conf.get("max_stake_usd", 5))
        self.daily_loss_limit = float(conf.get("daily_loss_limit_usd", 25))
        self.max_consecutive = int(conf.get("max_consecutive_losses", 5))
        self.max_exposure = float(conf.get("max_total_exposure_usd", 15))
        self.min_balance = float(conf.get("min_wallet_balance_usd", 10))
        self.max_api_errors = int(conf.get("max_api_errors", 3))
        self.api_error_window_s = float(conf.get("api_error_window_s", 300))
        self.state = state or RiskState()

    # ---------------------------------------------------------------- day roll
    def roll_day(self, now: float) -> None:
        d = local_day(now)
        if self.state.day != d:
            self.state.day, self.state.day_pnl, self.state.day_trades = d, 0.0, 0
            if self.state.day_stopped_until and now >= self.state.day_stopped_until:
                self.state.day_stopped_until = 0.0

    # ---------------------------------------------------------------- events
    def halt(self, reason: str) -> None:
        self.state.halted, self.state.halt_reason = True, reason

    def resume(self) -> None:
        self.state.halted, self.state.halt_reason = False, ""
        self.state.consecutive_losses = 0
        self.state.api_errors.clear()

    def record_close(self, pnl: float, now: float) -> list[str]:
        """Register a closed trade. Returns alerts to raise (e.g. limits hit)."""
        self.roll_day(now)
        s = self.state
        s.day_pnl += pnl
        s.day_trades += 1
        s.consecutive_losses = s.consecutive_losses + 1 if pnl < 0 else 0
        alerts = []
        if s.day_pnl <= -self.daily_loss_limit and not s.day_stopped_until:
            s.day_stopped_until = next_local_midnight(now)
            alerts.append(f"Daily loss limit hit ({s.day_pnl:+.2f} <= -{self.daily_loss_limit:g}). "
                          "No new trades until tomorrow.")
        if s.consecutive_losses >= self.max_consecutive and not s.halted:
            self.halt(f"{s.consecutive_losses} losses in a row")
            alerts.append(f"Paused after {s.consecutive_losses} consecutive losses. "
                          "Review, then press Resume.")
        return alerts

    def record_api_error(self, now: float) -> str | None:
        s = self.state
        s.api_errors = [t for t in s.api_errors if now - t < self.api_error_window_s] + [now]
        if len(s.api_errors) >= self.max_api_errors and not s.halted:
            self.halt(f"{len(s.api_errors)} API errors in {self.api_error_window_s / 60:.0f} min")
            return s.halt_reason
        return None

    def check_balance(self, balance: float | None) -> str | None:
        if balance is not None and balance < self.min_balance and not self.state.halted:
            self.halt(f"wallet balance ${balance:.2f} below ${self.min_balance:g}")
            return self.state.halt_reason
        return None

    # ---------------------------------------------------------------- gates
    def entry_blockers(self, *, now: float, stake: float, open_exposure: float,
                       window_busy: bool, degraded: bool) -> list[str]:
        """Reasons a NEW entry is not allowed right now (empty list = allowed)."""
        self.roll_day(now)
        s = self.state
        out = []
        if s.halted:
            out.append(f"HALTED: {s.halt_reason}")
        if s.day_stopped_until and now < s.day_stopped_until:
            out.append(f"daily loss limit hit ({s.day_pnl:+.2f}); resumes tomorrow")
        if s.maintenance_until and now < s.maintenance_until:
            out.append("Polymarket matching engine maintenance")
        if degraded:
            out.append("degraded data")
        if stake > self.max_stake + 1e-9:
            out.append(f"stake ${stake:.2f} > max ${self.max_stake:g}")
        if open_exposure + stake > self.max_exposure + 1e-9:
            out.append(f"exposure ${open_exposure:.2f} + ${stake:.2f} > max ${self.max_exposure:g}")
        if window_busy:
            out.append("already have a position/order in this window")
        return out

    def exits_allowed(self) -> bool:
        """Exits keep running under every pause except the kill switch."""
        return not (self.state.halted and self.state.halt_reason.startswith("KILL SWITCH"))

    def summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        self.roll_day(now)
        s = self.state
        return {**s.to_dict(),
                "daily_loss_limit": self.daily_loss_limit,
                "distance_to_daily_limit": round(self.daily_loss_limit + s.day_pnl, 4),
                "max_stake": self.max_stake, "max_exposure": self.max_exposure,
                "max_consecutive_losses": self.max_consecutive,
                "day_stopped": bool(s.day_stopped_until and now < s.day_stopped_until)}
