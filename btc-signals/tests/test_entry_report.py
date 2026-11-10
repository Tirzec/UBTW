"""Entry diagnosis report: cheap entries explained, cheap vs expensive results, outcome backfill, doubled bets."""
import time

import httpx

from app.db import DB
from app.entry_report import analyse, format_report
from app.ingest.us_markets import us_slug
from app.trading.store import TradeStore


def _db(tmp_path):
    d = DB(tmp_path / "e.db")
    TradeStore(d, "live", "polymarket_us", "crypto_updown")
    return d


def test_report_cheap_vs_expensive_and_backfill(tmp_path):
    d = _db(tmp_path)
    s15 = int(time.time()) // 900 * 900 - 7200
    w5, w15, wx = us_slug("5m", s15 + 600), us_slug("15m", s15), us_slug("15m", s15 + 900)
    base = {"mode": "live", "venue": "polymarket_us", "module": "crypto_updown", "status": "closed",
            "strategy": "fair_value", "entry_fee": 0.0, "opened_ts": time.time() - 3600}
    d.insert("bot_positions", {**base, "slug": w5, "side": "UP", "entry_price": 0.16, "cost": 1.0, "pnl": -1.0,
                               "p_model": 0.31, "edge": 0.12, "time_left_s": 120, "shares": 6.25,
                               "reasons": '[{"text": "fair 0.310 vs ask 0.16"}]'})
    d.insert("bot_positions", {**base, "slug": w15, "side": "UP", "entry_price": 0.07, "cost": 0.96, "pnl": -0.96,
                               "p_model": 0.14, "edge": 0.06, "time_left_s": 200, "shares": 13.7})
    d.insert("bot_positions", {**base, "slug": wx, "side": "DOWN", "entry_price": 0.70, "cost": 1.0, "pnl": 0.25,
                               "p_model": 0.80, "edge": 0.06, "time_left_s": 300, "shares": 1.43, "side_won": 1})

    def gw(req):
        return httpx.Response(200, json={"market": {"assetPriceTerms": {"priceToBeat": {"value": "100"},
                                                                        "settlementPrice": {"value": "99"}}}})
    a = analyse(d, mode="live", days=1, fetch_outcomes=True, client=httpx.Client(transport=httpx.MockTransport(gw)))
    assert len(a["cheap"]) == 2 and a["below"]["n"] == 2 and a["above"]["n"] == 1
    assert a["below"]["side_won_rate"] == 0.0 and abs(a["below"]["avg_p_model"] - 0.225) < 1e-9
    assert a["below"]["pnl"] == -1.96 and a["above"]["win_rate"] == 1.0
    assert [r["side_won"] for r in d.query("SELECT side_won FROM bot_positions ORDER BY id")] == [0, 0, 1]
    c = a["cheap"][0]
    assert any("edge +0.120 >= needed 0.10 (5m)" in ch for ch in c["checks"])
    assert any("180s into the window" in ch for ch in c["checks"])
    txt = format_report(a)
    assert "true probability at entry 31.0%" in txt and "doubled-up" in txt          # 5m + 15m settle together
