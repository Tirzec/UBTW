import json

import pytest

from app.feeds.base import FeedHealth, evaluate_staleness
from app.feeds.manager import FeedManager
from app.feeds.quality import DataQualityMonitor
from app.feeds.sources import (FEED_TYPES, BinanceFeed, CoinbaseFeed, KrakenFeed, PolyBoltFeed,
                               PolymarketRTDSFeed, build_feed, parse_iso)
from app.feeds.base import PriceTick

# ---- wire formats (captured from live feeds, Nov 2026) ---------------------------


def test_rtds_snapshot_and_update():
    f = PolymarketRTDSFeed({"url": "x"}, "reference")
    snap = json.dumps({"topic": "crypto_prices", "type": "subscribe", "payload": {
        "symbol": "btc/usd", "data": [{"timestamp": 1794232498000, "value": 74629.3},
                                      {"timestamp": 1794232499000, "value": 74629.31}]}})
    ticks = f.parse(snap, 1794232500.0)
    assert [t.kind for t in ticks] == ["spot", "spot"] and ticks[1].ts == 1794232499.0
    upd = json.dumps({"topic": "crypto_prices_twap_sixty", "type": "update", "payload": {
        "full_accuracy_value": "74653002055026569379840", "symbol": "btc/usd",
        "timestamp": 1794232567000, "value": 74653.00205502658, "window_s": 60}})
    t, = f.parse(upd, 1794232568.0)
    assert t.kind == "twap" and t.window_s == 60 and t.price == pytest.approx(74653.002)
    other = json.dumps({"topic": "crypto_prices_chainlink", "payload": {"symbol": "eth/usd",
                        "timestamp": 1, "value": 1}})
    assert f.parse(other, 0) == []


def test_rtds_filter_has_no_spaces():
    # RTDS silently drops live updates if the filter JSON contains spaces
    f = PolymarketRTDSFeed({"url": "x"}, "reference")
    for m in f.subscribe_messages():
        assert m["subscriptions"][0]["filters"] == '{"symbol":"btc/usd"}'
        assert len(m["subscriptions"]) == 1   # one topic per connection


def test_coinbase_parse_maker_side_inverted():
    f = CoinbaseFeed({"url": "x"}, "spot")
    msg = json.dumps({"type": "match", "side": "sell", "size": "0.002", "price": "74641.82",
                      "time": "2026-11-09T13:55:57.179667580Z"})
    t, = f.parse(msg, 0)
    assert t.side == 1 and t.price == 74641.82 and t.size == 0.002   # maker sell = taker buy
    assert t.ts == pytest.approx(parse_iso("2026-11-09T13:55:57.179667Z"))
    assert f.parse(json.dumps({"type": "heartbeat"}), 0) == []


def test_kraken_parse():
    f = KrakenFeed({"url": "x"}, "spot")
    msg = json.dumps({"channel": "trade", "type": "update", "data": [
        {"symbol": "BTC/USD", "side": "sell", "price": 74600.1, "qty": 0.01,
         "timestamp": "2026-11-09T14:01:35.673911Z"}]})
    t, = f.parse(msg, 0)
    assert t.side == -1 and t.price == 74600.1
    assert f.parse(json.dumps({"channel": "heartbeat"}), 0) == []


def test_binance_parse():
    f = BinanceFeed({"url": "x"}, "spot")
    t, = f.parse(json.dumps({"e": "trade", "p": "74000.5", "q": "0.1", "T": 1794232500123, "m": True}), 0)
    assert t.side == -1 and t.ts == pytest.approx(1794232500.123)


def test_polybolt_requires_credentials_and_parses():
    f = PolyBoltFeed({"url": "x"}, "reference", secrets={})
    assert "POLY_API_KEY" in f.check_ready()
    f = PolyBoltFeed({"url": "x"}, "reference", secrets={"poly_api_key": "k", "poly_api_secret": "s",
                                                          "poly_api_passphrase": "p"})
    assert f.check_ready() == ""
    t, = f.parse(json.dumps({"v": 1, "channel": "price.crypto.twap", "seq": 3, "ts": 1788886177000,
                             "payload": {"symbol": "btcusd", "timestamp": 1788886177000,
                                         "value": "78803.715261094101516288", "window_seconds": 60}}), 0)
    assert t.kind == "twap" and t.price == pytest.approx(78803.7152)


def test_parse_iso_variants():
    base = parse_iso("2026-11-09T14:01:35.673911+00:00")
    assert parse_iso("2026-11-09T14:01:35.673911580Z") == pytest.approx(base)
    assert parse_iso("2026-11-09T14:01:35.673911Z") == pytest.approx(base)
    assert parse_iso("2026-11-09T14:01:35.6739Z") == pytest.approx(base - 0.000011)
    assert parse_iso("2026-11-09T14:01:35Z") == pytest.approx(base - 0.673911)
    assert parse_iso("2026-11-09T09:01:35.5-05:00") == pytest.approx(base - 0.173911)


def test_registry_and_unknown_type():
    assert {"polymarket_rtds", "polybolt", "coinbase", "kraken", "binance"} <= set(FEED_TYPES)
    with pytest.raises(ValueError):
        build_feed({"type": "nope"}, "spot", {}, {})


def test_per_feed_health_overrides():
    f = build_feed({"type": "kraken", "url": "x", "no_price_after_s": 45}, "spot", {},
                   {"stale_after_s": 5, "no_price_after_s": 5})
    assert f.no_price_after_s == 45 and f.stale_after_s == 5

# ---- staleness -----------------------------------------------------------------


def h(**kw):
    x = FeedHealth("t", "spot")
    for k, v in kw.items():
        setattr(x, k, v)
    return x


def test_staleness_rules():
    now = 1000.0
    assert evaluate_staleness(h(), now, 5, 5, 30) == (True, "no data yet")
    ok = h(last_tick=999, last_msg=999.5, last_tick_ts=998.9, last_change=990)
    assert evaluate_staleness(ok, now, 5, 5, 30) == (False, "")
    s, why = evaluate_staleness(h(last_tick=990, last_msg=990, last_tick_ts=990, last_change=990),
                                now, 5, 5, 30)
    assert s and "not responding" in why
    # heartbeats keep a quiet trade feed alive until no_price_after_s
    quiet = h(last_tick=985, last_msg=999.8, last_tick_ts=985, last_change=985)
    assert evaluate_staleness(quiet, now, 5, 5, 60, no_price_after_s=30) == (False, "")
    s, why = evaluate_staleness(quiet, now, 5, 5, 60, no_price_after_s=10)
    assert s and "no price" in why
    s, why = evaluate_staleness(h(last_tick=999, last_msg=999, last_tick_ts=990, last_change=999),
                                now, 5, 5, 30)
    assert s and "lagging" in why
    s, why = evaluate_staleness(h(last_tick=999, last_msg=999, last_tick_ts=999, last_change=950),
                                now, 5, 5, 30)
    assert s and "frozen" in why
    assert evaluate_staleness(h(disabled_reason="no creds"), now, 5, 5, 30) == (True, "no creds")

# ---- data quality ---------------------------------------------------------------


def test_quality_gap_trips_after_persistence_and_recovers():
    q = DataQualityMonitor({"max_gap_bps": 15, "gap_persist_s": 3, "recover_after_s": 10})
    kw = dict(chainlink_twap=None, spot_twap=None, synthetic_reference=False)
    assert not q.update(0, chainlink_spot=70000, spot_prices={"cb": 70001}, **kw).degraded
    # 20 bps gap: not degraded until it has persisted 3s
    assert not q.update(1, chainlink_spot=70140, spot_prices={"cb": 70000}, **kw).degraded
    assert not q.update(3, chainlink_spot=70140, spot_prices={"cb": 70000}, **kw).degraded
    st = q.update(4.1, chainlink_spot=70140, spot_prices={"cb": 70000}, **kw)
    assert st.degraded and "gap" in st.reasons[0] and st.gap_bps == pytest.approx(20, abs=0.1)
    # back within range: stays degraded until recover_after_s
    assert q.update(5, chainlink_spot=70001, spot_prices={"cb": 70000}, **kw).degraded
    assert not q.update(15.1, chainlink_spot=70001, spot_prices={"cb": 70000}, **kw).degraded


def test_quality_synthetic_reference_is_degraded():
    q = DataQualityMonitor({})
    st = q.update(0, chainlink_spot=None, chainlink_twap=None, spot_prices={"cb": 1},
                  spot_twap=1, synthetic_reference=True)
    assert st.degraded and "Coinbase-based estimate" in st.reasons[0]


def test_quality_uses_median_and_twap_basis():
    q = DataQualityMonitor({"max_gap_bps": 15, "gap_persist_s": 0})
    st = q.update(0, chainlink_spot=None, chainlink_twap=70000, spot_prices={"a": 70500, "b": 70001},
                  spot_twap=70000, synthetic_reference=False)
    assert st.gap_basis == "twap" and st.gap_bps == pytest.approx(0)
    assert st.degraded and "disagree" in st.reasons[0]   # 71 bps between exchanges


def test_quality_no_spot():
    q = DataQualityMonitor({})
    st = q.update(0, chainlink_spot=70000, chainlink_twap=None, spot_prices={}, spot_twap=None,
                  synthetic_reference=False)
    assert st.degraded and "no live spot" in st.reasons[0]

# ---- manager: failover + synthetic fallback ------------------------------------


CFG = {"feeds": {
    "reference": [{"type": "polymarket_rtds", "url": "x"}],
    "spot": [{"type": "coinbase", "url": "x"}, {"type": "kraken", "url": "x"}],
    "health": {"stale_after_s": 5, "max_lag_s": 5, "frozen_after_s": 0}},
    "data_quality": {"max_gap_bps": 15, "gap_persist_s": 0}}


def feed_tick(fm, name, kind, price, ts, size=1.0):
    f = next(x for x in fm.reference_feeds + fm.spot_feeds if x.name == name)
    f._wrap_emit(fm.on_tick)(PriceTick(name, kind, price, ts, ts, size=size))


def test_manager_failover_and_synthetic_fallback():
    fm = FeedManager(CFG)
    t = 1_000_000.0
    for i in range(70):
        feed_tick(fm, "coinbase", "trade", 70000 + i, t + i)
        feed_tick(fm, "kraken", "trade", 70001 + i, t + i)
        feed_tick(fm, "polymarket_rtds", "spot", 70000.5 + i, t + i)
        feed_tick(fm, "polymarket_rtds", "twap", 69990 + i, t + i)
    now = t + 69.5
    st = fm.tick_health(now)
    assert fm.active_reference == "polymarket_rtds" and fm.active_spot == "coinbase"
    assert not st.degraded
    assert not fm.reference(now).synthetic
    # Chainlink goes silent -> synthetic estimate from coinbase, degraded
    for i in range(70, 80):
        feed_tick(fm, "coinbase", "trade", 70000 + i, t + i)
        feed_tick(fm, "kraken", "trade", 70001 + i, t + i)
    now = t + 79.5
    st = fm.tick_health(now)
    ref = fm.reference(now)
    assert ref.synthetic and ref.source == "synthetic:coinbase" and st.degraded
    # coinbase drops too -> kraken becomes primary
    for i in range(80, 90):
        feed_tick(fm, "kraken", "trade", 70001 + i, t + i)
    fm.tick_health(t + 89.5)
    assert fm.active_spot == "kraken"


def test_manager_reference_twap_at_boundary():
    fm = FeedManager(CFG)
    feed_tick(fm, "polymarket_rtds", "twap", 74542.42, 1794234000.0)
    assert fm.reference_twap_at(1794234000) == (74542.42, False)
    assert fm.reference_twap_at(1794233000) is None


def test_seed_history_fills_spot_series(monkeypatch):
    import httpx

    candles = [{"timestamp": 1000 + i, "close": str(70000 + i)} for i in range(100)]

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            return httpx.Response(200, json={"series": [{"candles": candles}]},
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    import asyncio
    fm = FeedManager(CFG)
    fm.bars["coinbase"].add_trade(1050, 1.0, 1, 1)          # live data wins over seeded data
    n = asyncio.run(fm.seed_history())
    assert n == 199                                          # 100 for kraken + 99 for coinbase
    w = fm.bars["coinbase"].window(1099, 100)
    assert w.close[0] == 70000 and w.close[50] == 1.0 and w.close[-1] == 70099
