"""Parse each market's resolution rules and check them against what the model assumes.

The model assumes: UP iff Chainlink BTC/USD TWAP(lookback) at window end
>= the same TWAP at window start ("price to beat"). Any market whose rules
text or structured config differs is flagged and forced to NO TRADE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class RuleSpec:
    asset: str | None
    duration: str | None
    twap_enabled: bool | None
    twap_lookback_s: int | None
    resolution_mechanism: str | None
    source_url: str
    comparator: str | None          # ">=" if "greater than or equal to" is present
    raw_text: str
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


_LOOKBACK_FROM_URL = re.compile(r"twap-(\d+)s")


def parse_rules(market: dict) -> RuleSpec:
    cfgm = market.get("cryptoMarketConfig") or {}
    text = market.get("description") or ""
    src = market.get("resolutionSource") or ""
    lookback = cfgm.get("twapLookbackSeconds")
    if lookback is None:
        m = _LOOKBACK_FROM_URL.search(src) or _LOOKBACK_FROM_URL.search(text)
        lookback = int(m.group(1)) if m else None
    low = text.lower()
    comparator = ">=" if "greater than or equal to" in low else (
        ">" if "greater than" in low else None)
    return RuleSpec(
        asset=cfgm.get("asset"),
        duration=cfgm.get("duration"),
        twap_enabled=cfgm.get("twapEnabled"),
        twap_lookback_s=int(lookback) if lookback is not None else None,
        resolution_mechanism=market.get("resolutionMechanism"),
        source_url=src,
        comparator=comparator,
        raw_text=text,
    )


def validate_rules(spec: RuleSpec, expected: dict, duration: str) -> RuleSpec:
    mm: list[str] = []
    if spec.asset not in (None, "btc"):
        mm.append(f"asset is {spec.asset!r}, expected 'btc'")
    if spec.duration not in (None, duration):
        mm.append(f"duration is {spec.duration!r}, expected {duration!r}")
    if expected.get("twap_enabled", True) and spec.twap_enabled is not True:
        mm.append(f"twapEnabled is {spec.twap_enabled!r}, model assumes a TWAP")
    allowed = expected.get("twap_lookback_seconds", [60])
    if spec.twap_lookback_s not in allowed:
        mm.append(f"TWAP lookback is {spec.twap_lookback_s}s, model supports {allowed}")
    want_mech = expected.get("resolution_mechanism")
    if want_mech and spec.resolution_mechanism not in (None, want_mech):
        mm.append(f"resolutionMechanism is {spec.resolution_mechanism!r}, expected {want_mech!r}")
    sc = expected.get("source_contains", "chain.link")
    if sc and sc not in spec.source_url and sc not in spec.raw_text:
        mm.append(f"resolution source {spec.source_url!r} is not Chainlink")
    phrase = expected.get("comparator_phrase", "greater than or equal to")
    if phrase and phrase not in spec.raw_text.lower():
        mm.append(f"rules text lacks {phrase!r} (tie handling may differ)")
    if "chainlink" not in spec.raw_text.lower():
        mm.append("rules text does not mention Chainlink")
    spec.mismatches = mm
    return spec
