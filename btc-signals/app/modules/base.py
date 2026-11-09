"""Strategy-module interface. Every module (crypto_updown, sports, ...) produces the same Signal
and answers the same questions, so the executor, risk controls, kill switch, paper/live modes,
audit log and dashboard are shared. Add a category by implementing StrategyModule."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Reason:
    text: str
    source: str = ""            # URL or provider name
    value: float = 0.0          # contribution (probability points, log-odds, ...)
    ts: float = 0.0             # when the underlying information was published/observed

    def as_dict(self) -> dict:
        return {"text": self.text, "source": self.source, "value": round(self.value, 4), "ts": self.ts}


@dataclass
class Signal:
    module: str
    market_id: str              # venue market slug
    token: str                  # "<slug>:long" / "<slug>:short" (or venue token id)
    side: str                   # human label: "UP", "Lakers", "Over 225.5"
    fair_prob: float            # fair probability for `side`
    confidence: float           # 0..1
    edge: float                 # fair - executable price - fees - buffer
    reasons: list[Reason] = field(default_factory=list)
    group: str = ""             # correlation group (e.g. one game)
    league: Optional[str] = None
    bet_type: str = ""          # updown | moneyline | spread | total
    label: str = ""             # short label for alerts/tables (e.g. "5m", "NBA")
    starts_at: Optional[float] = None
    expires_at: Optional[float] = None
    stake_usd: Optional[float] = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        d["reasons"] = [r.as_dict() for r in self.reasons]
        return d


@dataclass
class MarketInfo:
    slug: str
    tick_size: float = 0.01
    min_size: float = 1.0
    fee_schedule: dict = field(default_factory=dict)
    condition_id: str = ""
    label: str = ""
    entry_deadline: Optional[float] = None   # cancel resting entries / stop entering after this
    hold_phase_from: Optional[float] = None  # after this, no more trading: hold to resolution
    seconds_left: Optional[float] = None     # for exit rules that care about time to close


class StrategyModule:
    """Base class with conservative defaults; modules override what they need."""

    name = "base"

    def signals(self, now: float) -> list[Signal]:
        return []

    def market(self, slug: str) -> Optional[MarketInfo]:
        return None

    def book(self, token: str) -> Optional[dict]:
        return None

    def fair(self, position: dict) -> tuple[Optional[float], bool]:
        """(fair probability of the held side, model trustworthy?)"""
        return None, False

    def exit_decision(self, position: dict, valuation, now: float):
        raise NotImplementedError

    def settlement_per_share(self, position: dict) -> Optional[float]:
        """$ per share the held side settled at (1, 0, 0.5 tie, LFMP void) or None if unresolved."""
        return None

    def entry_blockers(self, signal: Signal, active: list[dict], now: float) -> list[str]:
        """Module-specific blockers (correlated exposure, per-window limits, data quality)."""
        return []

    def degraded(self) -> bool:
        return False

    def snapshot(self) -> dict:
        return {}
