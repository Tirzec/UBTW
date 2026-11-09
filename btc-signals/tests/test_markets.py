import copy

import pytest

from app.ingest.calendar import EconEvent, active_blackouts, blackout_overlaps, parse_ff
from app.ingest.discovery import Window, apply_event, slug_for, window_start
from app.ingest.news import classify, parse_rss, sentiment_score
from app.ingest.polymarket_ws import BookStore
from app.rules import parse_rules, validate_rules

from .conftest import EXPECTED_RULES, RULES_TEXT, gamma_event


def test_slugs():
    assert window_start(1794232487, "5m") == 1794232200
    assert window_start(1794232487, "15m") == 1794231900
    assert slug_for("5m", 1794232200) == "btc-updown-5m-1794232200"


def test_rules_ok_for_live_template():
    for d in ("5m", "15m"):
        spec = validate_rules(parse_rules(gamma_event(d)["markets"][0]), EXPECTED_RULES, d)
        assert spec.ok, spec.mismatches
        assert spec.twap_lookback_s == 60 and spec.comparator == ">="


def test_rules_flag_30s_twap():
    spec = validate_rules(parse_rules(gamma_event("5m", lookback=30)["markets"][0]), EXPECTED_RULES, "5m")
    assert not spec.ok and any("30s" in m for m in spec.mismatches)


def test_rules_flag_text_changes():
    m = gamma_event("5m", text=RULES_TEXT.replace("greater than or equal to", "greater than"))["markets"][0]
    spec = validate_rules(parse_rules(m), EXPECTED_RULES, "5m")
    assert not spec.ok and spec.comparator == ">"
    m = copy.deepcopy(gamma_event("5m")["markets"][0])
    m["resolutionSource"] = "https://www.binance.com/en/trade/BTC_USDT"
    m["description"] = m["description"].replace("chain.link", "binance.com").replace("Chainlink", "Binance")
    assert not validate_rules(parse_rules(m), EXPECTED_RULES, "5m").ok
    m = copy.deepcopy(gamma_event("5m")["markets"][0])
    m["cryptoMarketConfig"]["twapEnabled"] = False
    assert not validate_rules(parse_rules(m), EXPECTED_RULES, "5m").ok


def test_rules_lookback_from_url_when_config_missing():
    m = gamma_event("5m")["markets"][0]
    del m["cryptoMarketConfig"]
    assert parse_rules(m).twap_lookback_s == 60


def test_apply_event_live_and_closed():
    w = apply_event(Window("5m", 0, 300, "s"), gamma_event("5m"), EXPECTED_RULES)
    assert w.found and w.token_up == "111" and w.token_down == "222" and w.accepting_orders
    assert w.fee_schedule["rate"] == 0.07 and w.rules.ok and w.outcome is None
    ev = gamma_event("5m", closed=True, outcome_prices=("0", "1"),
                     meta={"finalPrice": 74538.39, "priceToBeat": 74599.87})
    w = apply_event(Window("5m", 0, 300, "s"), ev, EXPECTED_RULES)
    assert w.outcome == "DOWN" and w.gamma_ptb == 74599.87 and w.final_price == 74538.39


def test_apply_event_outcome_order_independent():
    ev = gamma_event("5m")
    ev["markets"][0]["outcomes"] = '["Down", "Up"]'
    w = apply_event(Window("5m", 0, 300, "s"), ev, EXPECTED_RULES)
    assert w.token_up == "222" and w.token_down == "111"


def test_book_store_snapshot_and_deltas():
    bs = BookStore()
    bs.apply([{"market": "m", "asset_id": "A", "bids": [{"price": "0.01", "size": "100"},
                                                       {"price": "0.48", "size": "10"}],
               "asks": [{"price": "0.52", "size": "5"}, {"price": "0.99", "size": "100"}]}], now=1)
    b = bs.get("A")
    assert b["bids"][0] == (0.48, 10) and b["asks"][0] == (0.52, 5)
    bs.apply({"market": "m", "price_changes": [
        {"asset_id": "A", "price": "0.50", "size": "7", "side": "BUY"},
        {"asset_id": "A", "price": "0.52", "size": "0", "side": "SELL"}]}, now=2)
    b = bs.get("A")
    assert b["bids"][0] == (0.50, 7) and b["asks"][0] == (0.99, 100) and b["updated"] == 2
    assert bs.get("B") is None


def test_news_classifier():
    lbl, s, c, hits = classify("Bitcoin surges to record high as ETF inflows jump")
    assert lbl == "bullish" and c > 0.5 and "+record high" in hits
    lbl, s, c, _ = classify("Major exchange hacked, halts withdrawals; BTC plunges")
    assert lbl == "bearish" and c > 0.8
    lbl, _, c, _ = classify("Bitcoin developers meet in Lisbon")
    assert lbl == "neutral" and c <= 0.3
    # irrelevant topics get low confidence
    assert classify("Tesla stock surges on earnings")[2] < classify("Bitcoin surges on earnings")[2]


def test_rss_and_sentiment():
    xml = """<rss><channel><item><title>Bitcoin rallies after rate cut</title><link>a</link>
    <pubDate>Mon, 09 Nov 2026 14:00:00 +0000</pubDate></item>
    <item><title>Crypto lender goes bankrupt</title><link>b</link>
    <pubDate>Mon, 09 Nov 2026 13:00:00 +0000</pubDate></item></channel></rss>"""
    hs = parse_rss(xml, "x")
    assert [h.label for h in hs] == ["bullish", "bearish"]
    now = hs[0].published
    s = sentiment_score(hs, now, halflife_min=30)
    assert -1 <= s <= 1
    assert sentiment_score(hs[:1], now, 30) > sentiment_score(hs[:1], now + 3600, 30) > 0


def test_calendar_blackouts():
    items = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-10T08:30:00-05:00", "impact": "High"},
             {"title": "German ZEW", "country": "EUR", "date": "2026-11-10T05:00:00-05:00", "impact": "High"},
             {"title": "Retail", "country": "USD", "date": "2026-11-10T10:00:00-05:00", "impact": "Low"}]
    evs = parse_ff(items, ["USD"], ["High"])
    assert [e.title for e in evs] == ["CPI m/m"]
    t = evs[0].ts
    assert active_blackouts(evs, t - 4 * 60, 5, 10) and not active_blackouts(evs, t - 6 * 60, 5, 10)
    assert active_blackouts(evs, t + 9 * 60, 5, 10) and not active_blackouts(evs, t + 11 * 60, 5, 10)
    # a 15m window ending right after the release overlaps the blackout even if "now" doesn't
    assert blackout_overlaps(evs, t - 20 * 60, t + 60, 5, 10)
    assert not blackout_overlaps([EconEvent("x", t)], t - 3600, t - 1800, 5, 10)
