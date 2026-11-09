"""Data-quality monitor: cross-checks Chainlink against exchange spot.

Any active reason => DEGRADED => the dashboard shows a banner and every
signal becomes NO TRADE. Pure logic, driven once per second by FeedManager.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import median

log = logging.getLogger("quality")


def bps(a: float, b: float) -> float:
    return (a - b) / b * 1e4 if b else 0.0


@dataclass
class _Persist:
    """A condition that trips after `trip_s` and clears after `clear_s` of being false."""
    trip_s: float
    clear_s: float
    since_true: float | None = None
    since_false: float | None = None
    active: bool = False

    def update(self, cond: bool, now: float) -> bool:
        if cond:
            self.since_false = None
            if self.since_true is None:
                self.since_true = now
            if not self.active and now - self.since_true >= self.trip_s:
                self.active = True
        else:
            self.since_true = None
            if self.since_false is None:
                self.since_false = now
            if self.active and now - self.since_false >= self.clear_s:
                self.active = False
        return self.active


@dataclass
class QualityState:
    degraded: bool = True
    reasons: list[str] = field(default_factory=lambda: ["starting up"])
    gap_bps: float | None = None          # chainlink - spot median
    gap_basis: str = ""                   # "spot" or "twap"
    spot_disagreement_bps: float | None = None
    synthetic_reference: bool = False

    def as_dict(self) -> dict:
        return {
            "degraded": self.degraded,
            "reasons": self.reasons,
            "gap_bps": None if self.gap_bps is None else round(self.gap_bps, 2),
            "gap_basis": self.gap_basis,
            "spot_disagreement_bps": None if self.spot_disagreement_bps is None
            else round(self.spot_disagreement_bps, 2),
            "synthetic_reference": self.synthetic_reference,
        }


class DataQualityMonitor:
    def __init__(self, conf: dict):
        self.max_gap = float(conf.get("max_gap_bps", 15))
        self.max_disagree = float(conf.get("max_spot_disagreement_bps", 15))
        self.require_two = bool(conf.get("require_two_spot_sources", False))
        persist = float(conf.get("gap_persist_s", 3))
        recover = float(conf.get("recover_after_s", 10))
        self._gap = _Persist(persist, recover)
        self._disagree = _Persist(persist, recover)
        self.state = QualityState()
        self._last_reasons: list[str] = []

    def update(self, now: float, *, chainlink_spot: float | None, chainlink_twap: float | None,
               spot_prices: dict[str, float], spot_twap: float | None,
               synthetic_reference: bool) -> QualityState:
        reasons: list[str] = []
        st = QualityState(degraded=False, reasons=reasons, synthetic_reference=synthetic_reference)

        if synthetic_reference:
            reasons.append("Chainlink feed unavailable - using Coinbase-based estimate")
        if not spot_prices:
            reasons.append("no live spot exchange feed")
        elif self.require_two and len(spot_prices) < 2:
            reasons.append("only one spot exchange feed live")

        spot_mid = median(spot_prices.values()) if spot_prices else None
        gap = None
        if not synthetic_reference and spot_mid:
            if chainlink_spot:
                gap, st.gap_basis = bps(chainlink_spot, spot_mid), "spot"
            elif chainlink_twap and spot_twap:
                gap, st.gap_basis = bps(chainlink_twap, spot_twap), "twap"
        st.gap_bps = gap
        if self._gap.update(gap is not None and abs(gap) > self.max_gap, now):
            reasons.append(f"Chainlink vs spot gap {gap if gap is not None else float('nan'):+.1f} bps "
                           f"exceeds {self.max_gap:g} bps")

        if len(spot_prices) >= 2:
            vals = list(spot_prices.values())
            st.spot_disagreement_bps = bps(max(vals), min(vals))
        if self._disagree.update((st.spot_disagreement_bps or 0) > self.max_disagree, now):
            reasons.append(f"spot exchanges disagree by {st.spot_disagreement_bps:.1f} bps")

        st.degraded = bool(reasons)
        if reasons != self._last_reasons:
            if reasons:
                log.warning("DATA DEGRADED: %s", "; ".join(reasons))
            else:
                log.info("data quality OK again")
            self._last_reasons = list(reasons)
        self.state = st
        return st
