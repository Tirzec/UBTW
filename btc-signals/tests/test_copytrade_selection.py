"""Copy trading step 2: fills -> trades, trader exclusions, delayed-copy backtest."""
import pytest

from app.copytrade.intl import resolution_payout
from app.copytrade.selection import backtest_copy, group_fills, max_drawdown, score_trader

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
    lucky = _closed(58, 1.0) + _closed(2, 500.0)
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

    def price_after(cond, tok, t):
        if cond == "m3" and t > 9500:
            return 0.50
        return prices[cond]

    def market_info(cond, tok):
        return {"m1": {"end_ts": 1e12, "payout": 1.0}, "m2": {"end_ts": 1e12, "payout": 0.0},
                "m3": {"end_ts": 1e12, "payout": 0.0}, "m4": {"end_ts": 20000 + 1800, "payout": 1.0}}[cond]

    r = backtest_copy(entries, price_after=price_after, market_info=market_info, delays=(30,), slip=0.0,
                      fee_rate=0.0)[30]
    assert r["copied"] == 2
    assert r["skipped"] == {"price more than 3c worse": 1, "market ends within 1 h": 1}
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
