"""Copy trading step 2: fills -> trades, trader exclusions, delayed-copy backtest."""
import pytest

from app.copytrade.__main__ import market_info_from
from app.copytrade.intl import pick_price, resolution_payout
from app.copytrade.selection import FAST_CRYPTO, backtest_copy, group_fills, max_drawdown, score_trader

NOW = 2_000_000_000.0
DAY = 86400


def fill(t, tok="A", side="BUY", px=0.5, size=10, cond="c1", slug="nba-x"):
    return {"timestamp": t, "token_id": tok, "side": side, "price": px, "size": size, "condition_id": cond,
            "slug": slug, "event_slug": slug, "title": slug, "outcome": tok, "outcome_index": 0}


def test_group_fills_merges_bursts_and_vwaps():
    e = group_fills([fill(100, px=0.50, size=10), fill(130, px=0.60, size=10), fill(1000, px=0.7)])
    assert len(e) == 2
    assert e[0]["price"] == pytest.approx(0.55) and e[0]["size"] == 20 and e[0]["fills"] == 2


def _closed(n, pnl=1.0, last=NOW - DAY):
    return [{"realized_pnl": pnl, "avg_price": 0.5, "total_size": 10, "last_event_at": last} for _ in range(n)]


def test_score_excludes_market_makers_lucky_and_fast_crypto():
    since = NOW - 90 * DAY
    ok = [fill(since + i * 3600, tok=f"T{i}", cond=f"c{i}") for i in range(60)]
    s = score_trader(wallet="w", name="n", fills=ok, truncated=False, closed=_closed(60), pnl_points=[],
                     since=since, now=NOW)
    assert s["excluded"] == [] and s["entries"] == 60 and s["win_rate"] == 1.0
    mm = ok + [fill(since + i * 3600 + 10, tok=f"U{i}", cond=f"c{i}") for i in range(60)]   # both sides
    s = score_trader(wallet="w", name="n", fills=mm, truncated=False, closed=_closed(60), pnl_points=[],
                     since=since, now=NOW)
    assert any("market maker" in w for w in s["excluded"])
    lucky = _closed(58, 1.0) + _closed(2, 60.0)          # 1.0 x 58 = 58 < 120 from two bets
    s = score_trader(wallet="w", name="n", fills=ok, truncated=False, closed=lucky, pnl_points=[], since=since, now=NOW)
    assert any("1-2 bets" in w for w in s["excluded"])
    fast = [fill(since + i * 3600, tok=f"T{i}", cond=f"c{i}", slug="btc-updown-5m-123") for i in range(60)]
    s = score_trader(wallet="w", name="n", fills=fast, truncated=False, closed=_closed(60), pnl_points=[],
                     since=since, now=NOW)
    assert any("5/15-minute crypto" in w for w in s["excluded"])
    few = ok[:10]
    s = score_trader(wallet="w", name="n", fills=few, truncated=False, closed=_closed(10), pnl_points=[],
                     since=since, now=NOW)
    assert any("only 10 trades" in w for w in s["excluded"])
    hft = [fill(NOW - 3 * DAY + i * 60, tok=f"T{i}", cond=f"c{i}") for i in range(2000)]
    s = score_trader(wallet="w", name="n", fills=hft, truncated=True, closed=_closed(60), pnl_points=[],
                     since=since, now=NOW)
    assert any("high-frequency" in w for w in s["excluded"])


def test_max_drawdown():
    assert max_drawdown([0, 10, 4, 12, 2, 5]) == 10


def test_backtest_delay_3c_rule_fees_proportional_sell_and_resolution():
    entries = group_fills([
        fill(1000, tok="A", px=0.50, size=100, cond="m1"),          # copied at 0.52 -> wins at resolution
        fill(5000, tok="B", px=0.40, size=100, cond="m2"),          # price ran to 0.45: > 3c worse, skipped
        fill(9000, tok="C", px=0.30, size=100, cond="m3"),          # copied at 0.30, they sell half at 0.50
        fill(9500, tok="C", side="SELL", px=0.50, size=50, cond="m3"),
        fill(20000, tok="D", px=0.60, size=10, cond="m4"),          # market ends in 30 min: skipped
    ])
    prices = {"m1": 0.52, "m2": 0.45, "m3": 0.30, "m4": 0.6}

    def price_after(cond, tok, t, side="BUY", wallet=None):
        if cond == "m3" and t > 9500:
            return 0.50
        return prices[cond]

    def market_info(cond, tok):
        return {"m1": {"end_ts": 1e12, "payout": 1.0}, "m2": {"end_ts": 1e12, "payout": 0.0},
                "m3": {"end_ts": 1e12, "payout": 0.0}, "m4": {"end_ts": 20000 + 1800, "payout": 1.0}}[cond]

    r = backtest_copy(entries, price_after=price_after, market_info=market_info, delays=(30,), slip=0.0,
                      fee_rate=0.0)[30]
    assert r["copied"] == 2
    assert r["skipped"] == {"price more than 3c worse": 1, "market resolves within 1 h": 1}
    assert r["closed"] == 2
    # A: 1/0.52 shares -> $1 each; C: half sold at 0.50, other half resolves at 0
    exp = (1 / 0.52 - 1) + ((1 / 0.30) * 0.5 * 0.5 - 0.5) + (0 - 0.5)
    assert r["pnl"] == pytest.approx(exp, abs=0.006)
    assert r["avg_copy_cost"] == pytest.approx((0.02 + 0.0) / 2)
    r2 = backtest_copy(entries, price_after=price_after, market_info=market_info, delays=(30,), slip=0.0,
                       fee_rate=0.095)[30]
    assert r2["pnl"] < r["pnl"] and r2["fees"] > 0


def test_resolution_payout():
    m = {"closed": True, "umaResolutionStatus": "resolved", "clobTokenIds": '["1", "2"]', "outcomePrices": '["0", "1"]'}
    assert resolution_payout(m, "2") == 1.0 and resolution_payout(m, "1") == 0.0
    assert resolution_payout({**m, "closed": False}, "2") is None
    assert resolution_payout({**m, "outcomePrices": '["0.3", "0.7"]'}, "2") is None


def test_unredeemed_losers_count_in_win_rate_and_profit():
    since = NOW - 90 * DAY
    ok = [fill(since + i * 3600, tok=f"T{i}", cond=f"c{i}") for i in range(60)]
    losers = [{"status": "OPEN", "redeemable": True, "current_value": 0, "avg_price": 0.5, "total_size": 10,
               "realized_pnl": 0, "last_event_at": NOW - DAY} for _ in range(40)]
    s = score_trader(wallet="w", name="n", fills=ok, truncated=False, closed=_closed(60) + losers, pnl_points=[],
                     since=since, now=NOW)
    assert s["win_rate"] == pytest.approx(0.6) and s["realized_pnl"] == pytest.approx(60 - 200)
    assert "not profitable in the window" in s["excluded"]


def test_fast_crypto_pattern_is_anchored():
    for slug in ("btc-updown-15m-1730000000", "eth-updown-5m-1730000000", "bitcoin-up-or-down-15-min-nov-10"):
        assert FAST_CRYPTO.search(slug), slug
    for slug in ("will-mrbeast-hit-5m-subscribers-by-dec", "will-the-game-last-over-15-minutes", "nba-bos-phi"):
        assert not FAST_CRYPTO.search(slug), slug


def test_pick_price_uses_copier_side_and_ignores_the_trader():
    rows = [
        {"timestamp": 100, "token_id": "A", "side": "SELL", "price": 0.48, "proxy_wallet": "0xo"},   # bid hit
        {"timestamp": 101, "token_id": "A", "side": "BUY", "price": 0.52, "proxy_wallet": "0xme"},   # the trader
        {"timestamp": 102, "token_id": "B", "side": "SELL", "price": 0.47, "proxy_wallet": "0xo"},   # A ask 0.53
        {"timestamp": 103, "token_id": "A", "side": "BUY", "price": 0.51, "proxy_wallet": "0xo"},
    ]
    assert pick_price(lambda w: rows, "A", 100, "BUY", "0xME") == (102.0, pytest.approx(0.53))   # worst in 5 s
    assert pick_price(lambda w: rows, "A", 100, "SELL") == (100.0, pytest.approx(0.48))
    assert pick_price(lambda w: [], "A", 100, "BUY") is None


def test_sell_larger_than_seen_keeps_holding_instead_of_dumping():
    entries = group_fills([fill(1000, tok="A", px=0.50, size=10, cond="m1"),
                           fill(5000, tok="A", side="SELL", px=0.60, size=500, cond="m1")])   # pre-window shares
    r = backtest_copy(entries, price_after=lambda *a: 0.5, market_info=lambda c, t: {"end_ts": 1e12, "payout": 1.0},
                      delays=(10,), slip=0.0, fee_rate=0.0)[10]
    assert r["skipped"].get("sell larger than the position we saw (fraction unknown, kept holding)") == 1
    assert r["pnl"] == pytest.approx(1.0)                     # held to resolution: 2 shares x $1 - $1


def test_backtest_only_copies_inside_window_but_tracks_holdings_before_it():
    entries = group_fills([fill(1000, tok="A", px=0.50, size=100, cond="m1"),     # before copy window
                           fill(6000, tok="A", px=0.50, size=100, cond="m1"),     # copied
                           fill(9000, tok="A", side="SELL", px=0.60, size=100, cond="m1")])   # half of 200
    r = backtest_copy(entries, price_after=lambda c, t, ts, side="BUY", w=None: 0.5 if side == "BUY" else 0.6,
                      market_info=lambda c, t: {"end_ts": 1e12, "payout": 0.0}, delays=(10,), slip=0.0,
                      fee_rate=0.0, copy_from=5000)[10]
    assert r["copied"] == 1
    assert r["pnl"] == pytest.approx(2 * 0.5 * 0.6 - 0.5 - 0.5)   # half sold at 0.6, half resolves at 0


def test_sports_resolution_time_is_after_kickoff():
    m = {"endDate": "2026-11-13T19:00:00Z", "gameStartTime": "2026-11-13 19:00:00+00", "closed": False,
         "clobTokenIds": '["1", "2"]', "outcomePrices": '["0.4", "0.6"]'}
    info = market_info_from(m, "2")
    assert info["mark"] == 0.6 and info["payout"] is None
    import calendar, time as _t
    assert info["end_ts"] == calendar.timegm(_t.strptime("2026-11-13 22:00:00", "%Y-%m-%d %H:%M:%S"))


def test_pick_price_never_uses_a_print_after_the_window():
    rows = [{"timestamp": 100 + 3600, "token_id": "A", "side": "BUY", "price": 0.11, "proxy_wallet": "0xo"}]
    assert pick_price(lambda w: rows, "A", 100, "BUY") is None          # an hour later: unpriced, not 0.11


def test_market_trades_pages_back_because_the_api_ignores_start_end():
    from app.copytrade.intl import IntlData

    pages = {None: ([{"timestamp": t} for t in range(5000, 3000, -100)], "c1"),
             "c1": ([{"timestamp": t} for t in range(3000, 1000, -100)], "c2"),
             "c2": ([{"timestamp": t} for t in range(1000, 0, -100)], None)}
    calls = []

    class Fake(IntlData):
        def _get(self, url, params=None, tries=4):
            calls.append(params.get("cursor"))
            rows, nxt = pages[params.get("cursor")]
            return {"data": rows, "pagination": {"has_more": nxt is not None, "next_cursor": nxt}}

    api = Fake(client=object())
    got = api.market_trades("m", 2000, 2200)
    assert [r["timestamp"] for r in got] == [2200, 2100, 2000] and calls == [None, "c1"]
    api.market_trades("m", 2500, 2600)                                      # served from the cache
    assert calls == [None, "c1"]
    assert [r["timestamp"] for r in api.market_trades("m", 0, 150)] == [100] and calls == [None, "c1", "c2"]


def test_three_cent_rule_uses_only_the_fills_seen_by_copy_time():
    from app.copytrade.selection import price_seen
    e = group_fills([fill(1000, px=0.40, size=10), fill(1060, px=0.60, size=10)])[0]
    assert e["price"] == pytest.approx(0.50)
    assert price_seen(e, 30) == pytest.approx(0.40) and price_seen(e, 90) == pytest.approx(0.50)
