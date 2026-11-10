"""Copy trading steps 3-5: leader fill grouping, US market matching, copy rules, partial sells, auto-pause."""
import asyncio
import json
import time

import pytest

from app.copytrade import match_politics, match_sports
from app.copytrade.leaders import group_new
from app.copytrade.module import CopyTradeModule, copy_checks, floor_tick, load_traders
from app.trading.rules import ExitDecision


# ------------------------------------------------------------------ leaders
def test_group_new_vwaps_per_token_and_side():
    rows = [{"timestamp": 10, "token_id": "A", "side": "BUY", "price": 0.40, "size": 10, "transaction_hash": "1"},
            {"timestamp": 11, "token_id": "A", "side": "BUY", "price": 0.50, "size": 30, "transaction_hash": "2"},
            {"timestamp": 12, "token_id": "B", "side": "SELL", "price": 0.70, "size": 5, "transaction_hash": "3"},
            {"timestamp": 13, "token_id": "A", "side": "BUY", "price": 0.5, "size": 0, "transaction_hash": "4"}]
    out = group_new(rows, "0xw", "w", now=20)
    assert [(t.token_id, t.side) for t in out] == [("A", "BUY"), ("B", "SELL")]
    assert out[0].size == 40 and out[0].price == pytest.approx(0.475) and out[0].fills == 2
    assert out[0].lag_s == 10


def test_leader_watcher_baseline_never_copies_history():
    from app.copytrade.leaders import LeaderWatcher

    pages = [[{"timestamp": 1, "token_id": "A", "side": "BUY", "price": 0.5, "size": 1, "transaction_hash": "old"}],
             [{"timestamp": 5, "token_id": "A", "side": "BUY", "price": 0.6, "size": 2, "transaction_hash": "new"},
              {"timestamp": 1, "token_id": "A", "side": "BUY", "price": 0.5, "size": 1, "transaction_hash": "old"}]]

    class W(LeaderWatcher):
        async def _get(self, path, params):
            return {"data": pages.pop(0), "pagination": {"has_more": False}}

    w = W(client=None, traders=[{"wallet": "0xA", "name": "a"}])
    assert asyncio.run(w.poll("0xa")) == []                     # first poll = baseline
    got = asyncio.run(w.poll("0xa"))
    assert len(got) == 1 and got[0].price == 0.6 and got[0].size == 2


# ------------------------------------------------------------------ copy rules
BOOK = {"asks": [(0.52, 100.0)], "bids": [(0.50, 100.0)]}


def chk(**kw):
    a = dict(their_price=0.50, book=BOOK, tick=0.01, stake=1.0, max_worse=0.03, min_exit_depth_usd=5,
             exit_band=0.05, max_spread=0.05)
    a.update(kw)
    return copy_checks(**a)


def test_copy_checks_three_cents_liquidity_spread():
    limit, ask, why = chk()
    assert why == "" and limit == pytest.approx(0.53) and ask == 0.52
    assert "more than 3c worse" in chk(their_price=0.48)[2]                  # ask 0.52 > 0.48 + 0.03
    assert chk(book={"asks": [(0.52, 100.0)], "bids": []})[2].startswith("no buyers")
    assert "spread" in chk(book={"asks": [(0.52, 100.0)], "bids": [(0.40, 100.0)]})[2]
    assert "of buyers within" in chk(book={"asks": [(0.52, 100.0)], "bids": [(0.50, 2.0)]})[2]
    assert "for sale" in chk(book={"asks": [(0.52, 1.0)], "bids": [(0.50, 100.0)]})[2]
    assert chk(book=None)[2] == "no sellers on Polymarket US"


def test_floor_tick():
    assert floor_tick(0.5349, 0.005) == pytest.approx(0.53) and floor_tick(0.53, 0.01) == pytest.approx(0.53)


# ------------------------------------------------------------------ sports matching
def _iev(start="2026-11-13T01:15:00Z"):
    return {"slug": "nfl-was-nyg-2026-11-13", "usId": None, "startTime": start,
            "teams": [{"name": "Washington Commanders", "alias": "Commanders", "abbreviation": "was",
                       "providerId": 1, "ordering": "away"},
                      {"name": "New York Giants", "alias": "Giants", "abbreviation": "nyg", "providerId": 2,
                       "ordering": "home"}]}


def _ue():
    was = {"name": "Washington Commanders", "abbreviation": "was", "providerId": 1, "ordering": "away"}
    nyg = {"name": "New York Giants", "abbreviation": "nyg", "providerId": 2, "ordering": "home"}
    st = "2026-11-13T01:15:00Z"
    return {"startTime": st, "markets": [
        {"slug": "aec-nfl-was-nyg-2026-11-12", "sportsMarketType": "football_team_full_game_winner",
         "gameStartTime": st, "marketSides": [{"long": True, "description": "Commanders", "team": was},
                                              {"long": False, "description": "Giants", "team": nyg}]},
        {"slug": "asc-nfl-was-nyg-2026-11-12-neg-1pt5", "sportsMarketType": "football_team_full_game_spread",
         "line": -1.5, "gameStartTime": st, "marketSides": [{"long": True, "description": "-1.50", "team": was},
                                                            {"long": False, "description": "+1.50", "team": nyg}]},
        {"slug": "tsc-nfl-was-nyg-2026-11-12-total-41pt5", "sportsMarketType": "football_team_full_game_total",
         "line": 41.5, "gameStartTime": st, "marketSides": [{"long": True, "description": "Over"},
                                                            {"long": False, "description": "Under"}]}]}


def test_map_market_spread_total_moneyline_and_refusals():
    iev, ue = _iev(), _ue()
    spread = {"sportsMarketType": "spreads", "line": -1.5, "outcomes": '["Commanders", "Giants"]',
              "slug": "nfl-was-nyg-2026-11-13-spread-away-1pt5"}
    r = match_sports.map_market(spread, iev, ue, 1, "Giants")                 # Giants +1.5
    assert r["token"] == "asc-nfl-was-nyg-2026-11-12-neg-1pt5:short"
    assert match_sports.map_market(spread, iev, ue, 0, "Commanders")["leg"] == "long"
    tot = {"sportsMarketType": "totals", "line": 41.5, "outcomes": '["Over", "Under"]'}
    assert match_sports.map_market(tot, iev, ue, 1, "Under")["token"].endswith("total-41pt5:short")
    ml = {"sportsMarketType": "moneyline", "outcomes": '["Commanders", "Giants"]'}
    assert match_sports.map_market(ml, iev, ue, 0, "Commanders")["token"] == "aec-nfl-was-nyg-2026-11-12:long"
    with pytest.raises(match_sports.NoMatch, match="not listed"):                  # never the nearest line
        match_sports.map_market({**tot, "line": 42.5}, iev, ue, 0, "Over")
    with pytest.raises(match_sports.NoMatch, match="whole-number"):
        match_sports.map_market({**spread, "line": -2.0}, iev, ue, 0, "Commanders")
    with pytest.raises(match_sports.NoMatch, match="kickoff"):
        match_sports.map_market(spread, _iev("2026-11-14T01:15:00Z"), ue, 1, "Giants")
    bad = _iev()
    bad["teams"][0]["providerId"] = 99
    with pytest.raises(match_sports.NoMatch, match="teams differ"):
        match_sports.map_market(spread, bad, ue, 1, "Giants")


def test_us_event_candidates_use_et_date_and_code_aliases():
    iev = {"slug": "nba-sac-nop-2026-10-30", "startTime": "2026-10-30T00:00:00Z"}
    c = match_sports.us_event_candidates(iev)
    assert c[0] == "nba-sac-no-2026-10-29"                                     # 8 pm ET on the 29th
    assert "nba-sac-nop-2026-10-30" in c


# ------------------------------------------------------------------ politics matching
def test_politics_match_strict_yes_no_sides():
    ev = {"title": "NH-01 Democratic Primary", "endDate": "2026-09-08T00:00:00Z"}
    m = {"question": "Will Stefany Shaheen be the Democratic nominee for NH-01?", "outcomes": '["Yes", "No"]',
         "description": "The primary election will take place on September 8, 2026.", "groupItemTitle": "Shaheen"}
    us_ev = {"ticker": "ushrp-nh-01-2026-09-08-dem", "title": "NH-01 Democratic Primary", "endDate": "2026-09-09",
             "markets": [{"slug": "enwc-ushrp-nh-01-2026-09-08-dem-stesha", "title": "Stefany Shaheen",
                          "status": "MARKET_STATUS_OPEN", "endDate": "2026-09-09T00:00:00Z",
                          "description": "This market will settle to Yes if Stefany Shaheen wins the Democratic "
                                         "nomination for NH-01.",
                          "marketSides": [{"long": True, "description": "Yes"}, {"long": False, "description": "No"}]}]}
    idx = match_politics.build_index([us_ev, us_ev])                          # duplicates are ignored
    r, why = match_politics.match(ev, m, idx, "No")
    assert why == "" and r["token"] == "enwc-ushrp-nh-01-2026-09-08-dem-stesha:short" and r["confidence"] == 1.0
    other = {**m, "question": "Will Maura Sullivan be the Democratic nominee for NH-01?"}
    assert match_politics.match(ev, other, idx, "Yes")[0] is None


# ------------------------------------------------------------------ module: sells, auto-pause, config
class _Store:
    def __init__(self, positions=(), state=None):
        self.positions = list(positions)
        self.state = dict(state or {})
        self.events = []
        self.mode, self.venue = "paper", "polymarket_us"

    def active_positions(self):
        return [p for p in self.positions if p["status"] in ("opening", "open", "closing", "resolving")]

    def get_state(self, k, default=None):
        return self.state.get(k, default)

    def set_state(self, k, v):
        self.state[k] = v

    def event(self, *a, **k):
        self.events.append(a)


class _Eng:
    def __init__(self):
        self.alerts = []

    def alert(self, *a):
        self.alerts.append(a)


def _module(store, traders=None):
    cfg = {"copytrade": {"traders": traders or [{"name": "T", "wallet": "0xAA"}], "pause_after_trades": 30}}
    m = CopyTradeModule(_Eng(), cfg, client=object())
    m.attach(store)
    return m


class _Val:
    shares_sellable = 10


def test_leader_sell_sells_same_fraction_and_unknown_fraction_holds():
    from app.copytrade.leaders import LeaderTrade
    p = {"id": 7, "status": "open", "shares": 4.0, "exit_shares": 0.0, "copy_leader": "0xaa",
         "copy_src_token": "TOK", "slug": "s", "token": "s:long"}
    m = _module(_Store([p]))
    t = LeaderTrade(wallet="0xaa", name="T", side="SELL", token_id="TOK", condition_id="c", slug="x",
                    event_slug="e", title="t", outcome="Yes", outcome_index=0, size=50, price=0.6,
                    first_ts=time.time(), seen_ts=time.time(), sell_fraction=0.25)
    m._copy_sell(t)
    d = m.exit_decision(p, _Val(), time.time())
    assert d.action == "SELL" and d.shares == pytest.approx(1.0)
    p["exit_shares"] = 1.0                                                     # sold
    assert m.exit_decision(p, _Val(), time.time()).action == "HOLD"
    m2 = _module(_Store([dict(p, exit_shares=0.0)]))
    m2._copy_sell(LeaderTrade(**{**t.__dict__, "sell_fraction": None}))
    assert m2.exit_decision(p, _Val(), time.time()).action == "HOLD"
    assert m2.decisions[0]["action"] == "hold"


def test_auto_pause_after_30_losing_copies(monkeypatch):
    st = _Store()
    m = _module(st)
    monkeypatch.setattr(m, "trader_stats", lambda: {"0xaa": {"closed": 30, "pnl": -0.5}})
    m._check_auto_pause()
    assert "0xaa" in st.state["copy:paused"] and m.engine.alerts
    monkeypatch.setattr(m, "trader_stats", lambda: {"0xaa": {"closed": 29, "pnl": -5}})
    st.state = {}
    m._check_auto_pause()
    assert not st.state.get("copy:paused")


def test_load_traders_merges_config_and_approved_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "copy_traders.json").write_text(json.dumps(
        {"traders": [{"name": "B", "wallet": "0xBB"}, {"name": "dup", "wallet": "0xaa"}]}))
    t = load_traders({"_root": str(tmp_path), "copytrade": {"traders": [{"name": "A", "wallet": "0xAA"}]}})
    assert [(x["name"], x["wallet"]) for x in t] == [("A", "0xaa"), ("B", "0xbb")]


def test_exit_decision_carries_partial_size():
    assert ExitDecision("SELL", "x", "copy_sell", shares=1.5).shares == 1.5
    assert ExitDecision("SELL", "x").shares is None


def test_config_is_live_copy_trading_with_predictions_archived():
    import yaml
    c = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert c["trading"]["mode"] == "live" and c["copytrade"]["mode"] == "live"   # user's choice 2026-11-10
    assert c["trading"]["live_phase"] == 1                                     # $1 hard cap per trade
    assert c["modules"]["crypto_updown"]["enabled"] is False and c["sports"]["enabled"] is False
    assert c["predictions"]["enabled"] is False
    r = c["copytrade"]["risk"]
    assert r["max_total_exposure_usd"] == 10 and r["max_consecutive_losses"] == 5
    assert c["copytrade"]["stake_usd"] == 1.0 and c["copytrade"]["max_per_market_usd"] == 3
    assert {t["name"] for t in c["copytrade"]["traders"]} == {"ArielFilipino", "rigatohni"}


# ------------------------------------------------------------------ review fixes
def _sell(name_frac, token="TOK"):
    from app.copytrade.leaders import LeaderTrade
    return LeaderTrade(wallet="0xaa", name="T", side="SELL", token_id=token, condition_id="c", slug="x",
                       event_slug="e", title="t", outcome="Yes", outcome_index=0, size=50, price=0.6,
                       first_ts=time.time(), seen_ts=time.time(), sell_fraction=name_frac)


def test_successive_sells_compound_while_first_sale_waits():
    p = {"id": 1, "status": "open", "shares": 10.0, "exit_shares": 0.0, "copy_leader": "0xaa",
         "copy_src_token": "TOK", "slug": "s", "token": "s:long"}
    m = _module(_Store([p]))
    m._copy_sell(_sell(0.5))
    m._copy_sell(_sell(0.5))                                   # 50% of what they had left
    d = m.exit_decision(p, _Val(), time.time())
    assert d.shares == pytest.approx(7.5)                      # keep 25%, not sell everything


def test_sell_reaches_resting_and_queued_copies():
    p = {"id": 2, "status": "opening", "shares": 0.0, "exit_shares": 0.0, "copy_leader": "0xaa",
         "copy_src_token": "TOK", "slug": "s", "token": "s:long"}
    st = _Store([p])
    m = _module(st)
    from app.modules.base import Signal
    q = Signal(module="copytrade", market_id="s", token="s:long", side="x", fair_prob=0.5, confidence=1, edge=0,
               extra={"position_fields": {"copy_leader": "0xaa", "copy_src_token": "TOK"}})
    m.pending.append(q)
    m._copy_sell(_sell(0.5))
    assert m.keep["2"] == pytest.approx(0.5) and q.extra["keep"] == pytest.approx(0.5) and q in m.pending
    m._copy_sell(_sell(1.0))                                   # they are out: queued copy is dropped
    assert q not in m.pending and m.keep["2"] == 0.0
    p.update(status="open", shares=4.0)                        # resting order filled 4 shares
    assert m.exit_decision(p, _Val(), time.time()).shares == pytest.approx(4.0)


def test_one_order_filled_over_several_polls_is_one_copy():
    st = _Store()
    m = _module(st)
    from app.copytrade.leaders import LeaderTrade
    calls = []

    async def fake_map(t):
        calls.append(t)
        return None, "stop here"
    m._map = fake_map
    t = LeaderTrade(wallet="0xaa", name="T", side="BUY", token_id="TOK", condition_id="c", slug="x",
                    event_slug="e", title="t", outcome="Yes", outcome_index=0, size=5, price=0.5,
                    first_ts=time.time(), seen_ts=time.time())
    asyncio.run(m._copy_buy(t))
    asyncio.run(m._copy_buy(t))
    assert len(calls) == 1 and m.decisions[0]["action"] == "ignore"


def test_unpause_baseline_is_respected(monkeypatch):
    st = _Store(state={"copy:unpaused": {"0xaa": {"closed": 30, "pnl": -2.0}}})
    m = _module(st)
    monkeypatch.setattr(m, "trader_stats", lambda: {"0xaa": {"closed": 35, "pnl": -2.5}})
    m._check_auto_pause()
    assert not st.state.get("copy:paused")                     # only 5 copies since the unpause
    monkeypatch.setattr(m, "trader_stats", lambda: {"0xaa": {"closed": 60, "pnl": -2.5}})
    m._check_auto_pause()
    assert "0xaa" in st.state["copy:paused"]


def test_market_fallback_and_early_settlement():
    m = _module(_Store())
    assert m.market("s") is None
    m.meta["s"] = {"resolve_ts": time.time() + 86400}
    mi = m.market("s")
    assert mi is not None and mi.hold_phase_from > time.time() + 3600      # not loaded yet: safe defaults
    m.settled["s"] = 1.0
    assert m.market("s").hold_phase_from <= time.time()                     # resolved early: settle now


def test_stale_positions_do_not_halve_a_full_exit():
    from app.copytrade.leaders import LeaderWatcher
    t = _sell(None)
    t.raw = [{"timestamp": 1000}]

    class W(LeaderWatcher):
        async def _get(self, path, params):
            return {"data": [{"token_id": "TOK", "current_size": 50, "last_event_at": 900}]}   # not updated

    w = W(client=None, traders=[{"wallet": "0xaa", "name": "T"}])
    import app.copytrade.leaders as L
    real_sleep = L.asyncio.sleep

    async def no_sleep(s):
        return None
    L.asyncio.sleep = no_sleep
    try:
        asyncio.run(w._sell_fraction(t))
    finally:
        L.asyncio.sleep = real_sleep
    assert t.sell_fraction == 1.0                               # 50 sold, positions still showed them


# ------------------------------------------------------------------ second review round
def test_gateway_keeps_query_string_and_retries_429():
    import httpx
    seen = []

    def handler(req):
        seen.append(req.url)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"})
        return httpx.Response(200, json={"events": []})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        m = CopyTradeModule(_Eng(), {"copytrade": {"traders": [{"name": "T", "wallet": "0xaa"}]}}, client=client)
        return await m._gw("/v1/events?categories=politics&limit=100&offset=200")
    assert asyncio.run(run()) == {"events": []}
    q = dict(seen[-1].params)
    assert q["categories"] == "politics" and q["limit"] == "100" and q["offset"] == "200" and "_" in q
    assert len(seen) == 2


def test_data_error_does_not_use_up_the_entry():
    import httpx
    m = _module(_Store())
    from app.copytrade.leaders import LeaderTrade

    async def boom(t):
        raise httpx.ConnectTimeout("x")
    m._map = boom
    t = LeaderTrade(wallet="0xaa", name="T", side="BUY", token_id="TOK", condition_id="c", slug="x",
                    event_slug="e", title="t", outcome="Yes", outcome_index=0, size=5, price=0.5,
                    first_ts=time.time(), seen_ts=time.time())
    asyncio.run(m.on_trade(t))
    assert ("0xaa", "TOK") not in m.last_copy                  # their next fill can still be copied


def test_hold_phase_follows_the_us_market_not_the_estimate():
    m = _module(_Store())
    m.meta["s"] = {"resolve_ts": time.time() - 600, "league": "nfl"}          # estimated end already passed
    m.us_markets["s"] = {"status": "MARKET_STATUS_OPEN"}
    assert m.market("s").hold_phase_from > time.time()                         # overtime: still mirror sells
    m.us_markets["s"] = {"status": "MARKET_STATUS_CLOSED"}
    assert m.market("s").hold_phase_from <= time.time()


def test_sells_made_while_stopped_are_caught_up():
    from app.copytrade.leaders import LeaderWatcher
    rows = [{"timestamp": 500, "token_id": "TOK", "side": "SELL", "price": 0.7, "size": 10, "transaction_hash": "s"},
            {"timestamp": 100, "token_id": "TOK", "side": "BUY", "price": 0.5, "size": 10, "transaction_hash": "b"},
            {"timestamp": 600, "token_id": "OTHER", "side": "SELL", "price": 0.7, "size": 1, "transaction_hash": "o"}]

    class W(LeaderWatcher):
        async def _get(self, path, params):
            if path == "positions":
                return {"data": []}
            return {"data": rows, "pagination": {"has_more": False}}

    w = W(client=None, traders=[{"wallet": "0xaa", "name": "T"}])
    w.catchup = {("0xaa", "TOK"): 200}                          # our copy was opened at t=200
    out = asyncio.run(w.poll("0xaa"))
    assert [(t.side, t.token_id) for t in out] == [("SELL", "TOK")] and out[0].sell_fraction == 1.0


def test_resume_while_stopped_applies_at_startup(tmp_path):
    from app.db import DB
    from app.trading.store import TradeStore
    from app.trading.trader import AutoTrader
    from app.trading.broker import PaperBroker
    db = DB(str(tmp_path / "t.db"))
    st = TradeStore(db, "paper", "polymarket_us", "copytrade")
    st.set_state("risk:copytrade", {"halted": True, "halt_reason": "KILL SWITCH: x", "halted_at": 100.0})
    st.set_state("resume_requested", 200.0)

    class M(CopyTradeModule):
        pass
    mod = _module(st)
    t = AutoTrader(_Eng(), {}, PaperBroker(lambda k: None, lambda k, s: [], lambda k: {}), st, module=mod,
                   exec_conf={"risk": {}})
    assert not t.risk.state.halted
    st.set_state("risk:copytrade", {"halted": True, "halt_reason": "KILL SWITCH: y", "halted_at": 300.0})
    t2 = AutoTrader(_Eng(), {}, PaperBroker(lambda k: None, lambda k, s: [], lambda k: {}), st, module=mod,
                    exec_conf={"risk": {}})
    assert t2.risk.state.halted                                   # a STOP after the resume still wins
