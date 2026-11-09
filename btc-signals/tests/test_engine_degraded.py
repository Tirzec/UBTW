"""End-to-end: degraded data (Chainlink gap or synthetic fallback) forces NO TRADE."""
import asyncio

from app.config import load_config
from app.engine import Engine
from app.feeds.base import PriceTick
from app.ingest.discovery import Window, apply_event

from .conftest import EXPECTED_RULES, gamma_event

T0 = 1_794_234_000  # a real 5m boundary


def make_engine(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "t.db")
    cfg["venue"] = "polymarket_intl"       # this test drives the Chainlink (RTDS) feed path
    cfg["news"]["enabled"] = False
    cfg["calendar"]["enabled"] = False
    cfg["data_quality"].update(max_gap_bps=15, gap_persist_s=2, recover_after_s=5)
    for f in cfg["feeds"]["reference"] + cfg["feeds"]["spot"]:
        f["frozen_after_s"] = 0
    return Engine(cfg)


def tick(e, name, kind, px, ts):
    f = next(x for x in e.feeds.reference_feeds + e.feeds.spot_feeds if x.name == name)
    f._wrap_emit(e.feeds.on_tick)(PriceTick(name, kind, px, ts, ts, size=0.1, side=1))


def feed_second(e, ts, chainlink_px, spot_px, rtds=True):
    tick(e, "coinbase", "trade", spot_px, ts)
    tick(e, "kraken", "trade", spot_px + 0.5, ts)
    if rtds:
        tick(e, "polymarket_rtds", "spot", chainlink_px, ts)
        tick(e, "polymarket_rtds", "twap", chainlink_px, ts)


def setup_window(e, now):
    w = e.discovery.get("5m", T0)
    apply_event(w, gamma_event("5m"), EXPECTED_RULES)
    w.token_up, w.token_down = "UPTOK", "DNTOK"
    store = e.clob.store
    store.apply({"asset_id": "UPTOK", "bids": [{"price": "0.40", "size": "5000"}],
                 "asks": [{"price": "0.41", "size": "5000"}]}, now)
    store.apply({"asset_id": "DNTOK", "bids": [{"price": "0.59", "size": "5000"}],
                 "asks": [{"price": "0.60", "size": "5000"}]}, now)
    return w


def test_degraded_gap_and_synthetic_force_no_trade(tmp_path):
    async def run():
        e = make_engine(tmp_path)
        e._started = T0 - 10_000
        # 20 minutes of clean data, BTC sitting $60 above the price to beat at the end
        for s in range(T0 - 1200, T0 + 120):
            px = 70000.0 if s <= T0 else 70060.0
            feed_second(e, float(s), px, px)
        now = T0 + 120.5
        e.feeds.tick_health(now)
        w = setup_window(e, now)
        ev = e.evaluate(w, now)
        assert w.price_to_beat == 70000.0 and w.ptb_source == "chainlink"
        assert not e.feeds.quality.state.degraded
        assert ev.decision.action == "UP", ev.decision.blocked_by   # market 0.41 vs model >> 0.5

        # Chainlink drifts 30 bps away from Coinbase/Kraken for several seconds
        for s in range(T0 + 121, T0 + 126):
            feed_second(e, float(s), 70060.0 * 1.003, 70060.0)
            e.feeds.tick_health(s + 0.5)
        now = T0 + 125.5
        q = e.feeds.quality.state
        assert q.degraded and any("gap" in r for r in q.reasons)
        ev = e.evaluate(w, now)
        assert ev.decision.action == "NO TRADE"
        assert any(g.startswith("DEGRADED DATA") for g in ev.decision.blocked_by)
        snap = e.snapshot()
        assert snap["quality"]["degraded"] and snap["windows"]["5m"]["decision"]["action"] == "NO TRADE"

        # Chainlink feed dies entirely -> synthetic Coinbase estimate -> still degraded
        for s in range(T0 + 126, T0 + 140):
            feed_second(e, float(s), 0, 70060.0, rtds=False)
        now = T0 + 139.5
        e.feeds.tick_health(now)
        ref = e.feeds.reference(now)
        assert ref.synthetic and ref.source.startswith("synthetic:")
        ev = e.evaluate(w, now)
        assert ev.decision.action == "NO TRADE"
        assert any("Coinbase-based estimate" in g for g in ev.decision.blocked_by)
        await e.http.aclose()

    asyncio.run(run())
