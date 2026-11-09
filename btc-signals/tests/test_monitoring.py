"""Step 6: daily summary (trades, P&L after fees, win rate, avg win/loss, sports CLV) and live alerts."""
import time

from app.db import DB
from app.notify import LIVE_REQUIRED_KINDS, Notifier
from app.trading.store import TradeStore
from app.trading.summary import build_summary, format_summary


def test_daily_summary(tmp_path):
    d = DB(tmp_path / "s.db")
    TradeStore(d, "live", "polymarket_us", "crypto_updown")
    noon = time.mktime(time.strptime("2026-11-09 12:00", "%Y-%m-%d %H:%M"))
    rows = [("crypto_updown", 0.30, None), ("crypto_updown", -0.10, None), ("crypto_updown", -0.20, None),
            ("sports", 0.50, 0.04), ("sports", -0.40, -0.01), ("test_order", -0.05, None)]
    for i, (mod, pnl, clv) in enumerate(rows):
        d.insert("bot_positions", {"mode": "live", "status": "closed", "venue": "polymarket_us", "module": mod,
                                   "pnl": pnl, "won": int(pnl > 0), "cost": 1, "entry_fee": 0.01, "exit_fee": 0.02,
                                   "closed_ts": noon + i, "clv": clv})
    d.insert("bot_positions", {"mode": "live", "status": "closed", "venue": "polymarket_us", "module": "sports",
                               "pnl": 9.0, "cost": 1, "entry_fee": 0, "exit_fee": 0, "closed_ts": noon + 86400})
    s = build_summary(d, mode="live", date="2026-11-09")
    c, sp, t = s["modules"]["crypto_updown"], s["modules"]["sports"], s["total"]
    assert c["trades"] == 3 and abs(c["pnl"] - 0.0) < 1e-9 and abs(c["win_rate"] - 1 / 3) < 1e-9
    assert abs(c["avg_win"] - 0.30) < 1e-9 and abs(c["avg_loss"] + 0.15) < 1e-9
    assert sp["trades"] == 2 and abs(sp["clv_avg"] - 0.015) < 1e-9 and sp["clv_beat"] == 1
    assert t["trades"] == 5                                  # test orders and other days excluded
    title, body = format_summary(s)
    assert "5 trades" in title and "after fees" in title
    assert "Sports:" in body and "CLV +0.015 avg (1/2 beat the close)" in body and "avg win" in body


def test_empty_day_summary(tmp_path):
    d = DB(tmp_path / "e.db")
    TradeStore(d, "paper", "polymarket_us")
    title, body = format_summary(build_summary(d, mode="paper", date="2026-11-09"))
    assert "0 trades" in title and "win rate -" in body


def test_live_alert_kinds_cannot_be_turned_off(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "t")
    n = Notifier({"notifications": {"kinds": ["signal"]}})
    assert not n.wants({"kind": "error", "title": "x"})
    n.kinds |= LIVE_REQUIRED_KINDS                         # what the engine does in LIVE mode
    for k in ("trade", "risk", "error", "summary"):
        assert n.wants({"kind": k, "title": k})


def test_raising_daily_limit_lifts_todays_stop():
    from app.trading.risk import RiskManager, RiskState
    st = RiskState(day=time.strftime("%Y-%m-%d"), day_pnl=-1.21, day_stopped_until=time.time() + 3600)
    rm = RiskManager({"daily_loss_limit_usd": 5}, st)
    assert st.day_pnl > -rm.daily_loss_limit          # what AutoTrader._recompute_day checks on startup
    rm2 = RiskManager({"daily_loss_limit_usd": 1}, RiskState(day_pnl=-1.21, day_stopped_until=time.time() + 3600))
    assert not rm2.state.day_pnl > -rm2.daily_loss_limit   # old limit: stays stopped
