"""Sports module: de-vig, consensus, matching, parsers, budgets, rules, exits, exposure, CLV."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.db import DB
from app.modules.sports.markets import SportsGame, check_rules, classify_market, liquidity_ok, parse_event
from app.modules.sports.module import SportsModule
from app.modules.sports.odds import (Budget, Quote, SharpApiSource, TheOddsApiSource, TheRundownSource,
                                     american_to_prob, canonicalize, consensus, devig, map_teams, match_event,
                                     OddsEvent, team_score)
from app.trading.broker import PaperBroker
from app.trading.rules import value_position
from app.trading.store import TradeStore
from app.trading.trader import AutoTrader

ML_DESC = ("This market will settle to the winner of the LA Lakers vs ATL Hawks professional basketball game "
           "scheduled for November 9, 2026 at 7:00PM ET. Overtime is included if played. If the game is delayed, "
           "postponed, suspended or otherwise rescheduled to a date and time not within two days of the originally "
           "scheduled date and time, the market will settle to the last fair market price.")
SP_DESC = ML_DESC.replace("settle to the winner of", "settle to Yes if Los Angeles Lakers covers a +1.5 point spread in")
TOT_DESC = ML_DESC.replace("settle to the winner of", "settle to Yes if LA Lakers and ATL Hawks combine for over 225.5 points in")
LAL = {"name": "Los Angeles Lakers"}
ATL = {"name": "Atlanta Hawks"}


def gw_event(start_iso="2030-01-01T00:00:00Z"):
    """Shape copied from GET gateway.polymarket.us/v1/events/slug/nba-lal-atl-2026-11-09."""
    return {"slug": "nba-lal-atl", "title": "LA Lakers vs. ATL Hawks", "startDate": start_iso, "markets": [
        {"slug": "aec-nba-lal-atl", "sportsMarketType": "basketball_team_full_game_winner", "status": "MARKET_STATUS_OPEN",
         "line": None, "description": ML_DESC, "feeCoefficient": 0.095, "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01,
         "marketSides": [{"description": "Lakers", "long": True, "team": LAL}, {"description": "Hawks", "long": False, "team": ATL}]},
        {"slug": "asc-nba-lal-atl-pos-1pt5", "sportsMarketType": "basketball_team_full_game_spread", "status": "MARKET_STATUS_OPEN",
         "line": 1.5, "description": SP_DESC, "feeCoefficient": 0.095,
         "marketSides": [{"description": "+1.50", "long": True, "team": LAL}, {"description": "-1.50", "long": False, "team": ATL}]},
        {"slug": "tsc-nba-lal-atl-225pt5", "sportsMarketType": "basketball_team_full_game_total", "status": "MARKET_STATUS_OPEN",
         "line": 225.5, "description": TOT_DESC, "feeCoefficient": 0.095,
         "marketSides": [{"description": "Over", "long": True}, {"description": "Under", "long": False}]},
        {"slug": "astatc-prop", "sportsMarketType": "basketball_player_points", "status": "MARKET_STATUS_OPEN",
         "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}]},
        {"slug": "atc-1h", "sportsMarketType": "basketball_team_first_half_winner", "status": "MARKET_STATUS_OPEN",
         "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}]},
    ]}


# ------------------------------------------------------------------ maths
def test_american_and_devig():
    assert american_to_prob(-110) == pytest.approx(110 / 210)
    assert american_to_prob(150) == pytest.approx(0.4)
    p = devig([american_to_prob(-110), american_to_prob(-110)])
    assert p == pytest.approx([0.5, 0.5])
    fav, dog = devig([american_to_prob(-300), american_to_prob(240)])
    mfav, mdog = devig([american_to_prob(-300), american_to_prob(240)], "multiplicative")
    assert fav + dog == pytest.approx(1) and fav > mfav          # power keeps more on the favourite
    three = devig([0.5, 0.3, 0.27])
    assert sum(three) == pytest.approx(1)


def q(book, market, team, line, price, ts=1000.0, source="therundown"):
    return Quote(source, book, market, team, line, price, ts, ts)


def test_consensus_needs_both_sides_exact_line_and_averages_books():
    quotes = [q("draftkings", "moneyline", "Los Angeles Lakers", None, -120), q("draftkings", "moneyline", "Atlanta Hawks", None, 100),
              q("fanduel", "moneyline", "Los Angeles Lakers", None, -130), q("fanduel", "moneyline", "Atlanta Hawks", None, 110),
              q("betmgm", "moneyline", "Los Angeles Lakers", None, -125)]          # one side only -> ignored
    f = consensus(quotes, "moneyline", "Los Angeles Lakers", None, other_team="Atlanta Hawks", now=1100)
    assert f.n_books == 2 and set(f.books) == {"draftkings", "fanduel"}
    assert f.prob == pytest.approx((devig([american_to_prob(-120), american_to_prob(100)])[0] +
                                    devig([american_to_prob(-130), american_to_prob(110)])[0]) / 2)
    sp = [q("draftkings", "spread", "Los Angeles Lakers", 1.5, -110), q("draftkings", "spread", "Atlanta Hawks", -1.5, -110)]
    assert consensus(sp, "spread", "Los Angeles Lakers", 1.5, other_team="Atlanta Hawks", now=1100).prob == pytest.approx(0.5)
    assert consensus(sp, "spread", "Los Angeles Lakers", 2.5, other_team="Atlanta Hawks", now=1100) is None   # exact line
    tot = [q("fanduel", "total", "over", 225.5, -105), q("fanduel", "total", "under", 225.5, -115)]
    assert 0.48 < consensus(tot, "total", "over", 225.5, now=1100).prob < 0.5
    assert consensus(quotes, "moneyline", "Los Angeles Lakers", None, other_team="Atlanta Hawks",
                     now=1000 + 99999, max_age_s=600) is None                       # stale quotes dropped


def test_newest_quote_per_book_wins_across_sources():
    old = q("draftkings", "moneyline", "Los Angeles Lakers", None, -200, ts=1000)
    new = q("draftkings", "moneyline", "Los Angeles Lakers", None, -120, ts=2000, source="sharpapi")
    other = q("draftkings", "moneyline", "Atlanta Hawks", None, 100, ts=2000, source="sharpapi")
    f = consensus([old, new, other], "moneyline", "Los Angeles Lakers", None, other_team="Atlanta Hawks", now=2100)
    assert f.prob == pytest.approx(devig([american_to_prob(-120), american_to_prob(100)])[0])


def test_team_matching_handles_abbreviations_and_shared_nicknames():
    assert team_score("PHO Suns", "Phoenix Suns") >= 0.5
    m = map_teams(["LA Lakers", "ATL Hawks"], ["Atlanta Hawks", "Los Angeles Lakers"])
    assert m == {"LA Lakers": "Los Angeles Lakers", "ATL Hawks": "Atlanta Hawks"}
    m = map_teams(["Man United", "Newcastle United"], ["Newcastle United", "Manchester United"])
    assert m == {"Man United": "Manchester United", "Newcastle United": "Newcastle United"}
    assert map_teams(["Boston Celtics", "Miami Heat"], ["Los Angeles Lakers", "Atlanta Hawks"]) is None
    evs = [OddsEvent("x", "1", "nba", "Atlanta Hawks", "Los Angeles Lakers", 1000.0),
           OddsEvent("x", "2", "nba", "Atlanta Hawks", "Los Angeles Lakers", 1000.0 + 5 * 86400)]
    assert match_event(evs, ["Los Angeles Lakers", "Atlanta Hawks"], 1000.0 + 600).event_id == "1"
    cq = canonicalize([q("fanduel", "moneyline", "LA Lakers", None, -110), q("fanduel", "total", "over", 220.5, -110)],
                      {"LA Lakers": "Los Angeles Lakers"})
    assert cq[0].team == "Los Angeles Lakers" and cq[1].team == "over"


# ------------------------------------------------------------------ provider parsers (documented examples)
def test_therundown_parse_matches_documented_v2_shape():
    d = {"events": [{"event_id": "abc", "event_date": "2030-01-01T00:00:00Z",
                     "teams": [{"name": "Los Angeles Lakers"}, {"name": "Atlanta Hawks"}],
                     "score": {"event_status": "STATUS_SCHEDULED"},
                     "markets": [{"market_id": 1, "period_id": 0, "participants": [
                         {"id": 23, "type": "TYPE_TEAM", "name": "Los Angeles Lakers", "lines": [
                             {"value": "", "prices": {"19": {"price": 150, "is_main_line": True, "updated_at": "2027-02-02T23:30:00Z"},
                                                      "23": {"price": 155, "is_main_line": True, "updated_at": "2027-02-02T23:28:00Z"},
                                                      "3": {"price": 151}}}]}]},
                                 {"market_id": 3, "period_id": 0, "participants": [
                                     {"type": "TYPE_RESULT", "name": "Over", "lines": [
                                         {"value": "225.5", "prices": {"22": {"price": -110, "is_main_line": True}}}]}]},
                                 {"market_id": 1, "period_id": 1, "participants": []}]}]}
    ev, = TheRundownSource.parse_events(d, "nba", 5.0)
    assert ev.away == "Los Angeles Lakers" and ev.home == "Atlanta Hawks"
    books = {(x.book, x.market, x.team, x.line, x.american) for x in ev.quotes}
    assert ("draftkings", "moneyline", "Los Angeles Lakers", None, 150.0) in books
    assert ("fanduel", "moneyline", "Los Angeles Lakers", None, 155.0) in books
    assert ("betmgm", "total", "over", 225.5, -110.0) in books
    assert not any(x.book == "" or x.book.isdigit() for x in ev.quotes)            # Pinnacle (3) not in free tier


def test_sharpapi_parse_matches_documented_shape():
    d = {"data": [{"sportsbook": "draftkings", "event_id": "evt1", "home_team": "PHI 76ers", "away_team": "PHO Suns",
                   "market_type": "moneyline", "selection": "PHO Suns", "selection_type": "away", "odds_american": -150,
                   "line": None, "start_time": "2026-01-26T19:30:00Z", "timestamp": "2026-01-26T10:15:00.000Z"},
                  {"sportsbook": "fanduel", "event_id": "evt1", "home_team": "PHI 76ers", "away_team": "PHO Suns",
                   "market_type": "total_points", "selection": "Over 221.5", "selection_type": "over", "odds_american": -110,
                   "line": 221.5, "start_time": "2026-01-26T19:30:00Z", "timestamp": "2026-01-26T10:15:00.000Z"}]}
    ev, = SharpApiSource.parse_odds(d, "nba", 0)
    assert ev.home == "PHI 76ers" and {(x.book, x.market, x.team, x.line) for x in ev.quotes} == {
        ("draftkings", "moneyline", "PHO Suns", None), ("fanduel", "total", "over", 221.5)}


def test_theoddsapi_parse_event_odds():
    d = {"id": "e1", "home_team": "Atlanta Hawks", "away_team": "Los Angeles Lakers", "commence_time": "2030-01-01T00:00:00Z",
         "bookmakers": [{"key": "pinnacle", "last_update": "2030-01-01T00:00:00Z", "markets": [
             {"key": "spreads", "outcomes": [{"name": "Los Angeles Lakers", "price": -105, "point": 1.5},
                                             {"name": "Atlanta Hawks", "price": -115, "point": -1.5}]}]}]}
    ev = TheOddsApiSource.parse_event_odds(d, "nba", 0)
    f = consensus(ev.quotes, "spread", "Los Angeles Lakers", 1.5, other_team="Atlanta Hawks", now=ev.quotes[0].updated)
    assert set(f.books) == {"pinnacle"} and 0.47 < f.prob < 0.5     # -105 is the less likely side vs -115


# ------------------------------------------------------------------ budgets never exceed caps
class MemStore:
    def __init__(self):
        self.s = {}

    def get_state(self, k, d=None):
        return self.s.get(k, d)

    def set_state(self, k, v):
        self.s[k] = v


def test_budget_hard_caps_and_persistence():
    st = MemStore()
    b = Budget("theoddsapi", st, {"month": 500, "reserve": 25})
    spent = 0
    while b.can_spend(1):
        b.spend(1)
        spent += 1
    assert spent == 475                                         # stops at cap - reserve, never over
    b2 = Budget("theoddsapi", st, {"month": 500, "reserve": 25})  # restart: ledger reloads
    assert not b2.can_spend(1)
    b3 = Budget("x", MemStore(), {"day": 100})
    b3.spend(10, remaining_header="5")                          # provider says only 5 left
    assert not b3.can_spend(10) and b3.can_spend(5)


def test_pinnacle_check_refuses_when_budget_exhausted():
    src = TheOddsApiSource({"key_env": "NOPE_KEY", "budget": {"month": 500, "reserve": 25}}, MemStore())
    src.key = "k"
    src.budget.state = {"month": time.strftime("%Y-%m", time.gmtime()), "month_used": 475,
                        "day": time.strftime("%Y-%m-%d", time.gmtime()), "day_used": 0}

    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("must not call the API when over budget")
    src.client = Boom()
    assert asyncio.run(src.pinnacle_check("nba", "e1", "spread")) is None
    assert "reserve" in src.last_error


# ------------------------------------------------------------------ markets and rules
def test_classify_and_rules():
    g = parse_event(gw_event(), "nba")
    assert set(m.bet_type for m in g.markets.values()) == {"moneyline", "spread", "total"}   # no props/periods
    assert len(g.markets) == 3 and all(m.rules_ok for m in g.markets.values())
    sp = g.markets["asc-nba-lal-atl-pos-1pt5"]
    assert sp.long_team == "Los Angeles Lakers" and sp.line == 1.5
    assert sp.side_label(sp.token_long) == "Lakers +1.5" and sp.side_label(sp.token_short) == "Hawks -1.5"
    tot = g.markets["tsc-nba-lal-atl-225pt5"]
    assert tot.side_label(tot.token_short) == "Under 225.5"
    bad = dict(gw_event()["markets"][0], description="Settles to the winner. Ask support for details.")
    assert check_rules(bad, "basketball")                       # ambiguous -> skipped
    assert not classify_market(bad, gw_event(), "nba").rules_ok


def test_liquidity_filter():
    assert liquidity_ok({"bids": [(0.48, 5000)], "asks": [(0.49, 5000)]}, 0.03, 200)[0]
    assert not liquidity_ok({"bids": [(0.40, 5000)], "asks": [(0.49, 5000)]}, 0.03, 200)[0]
    assert not liquidity_ok({"bids": [(0.48, 50)], "asks": [(0.49, 5000)]}, 0.03, 200)[0]
    assert not liquidity_ok(None, 0.03, 200)[0]


# ------------------------------------------------------------------ module end to end (paper)
SC = {"sports": {"enabled": True, "leagues": {"nba": {"enabled": True, "buffer": 0.05}}, "edge_buffer": 0.07,
                 "no_entry_before_start_min": 15, "liquidity": {"max_spread": 0.03, "min_depth": 200},
                 "entry": {"stake_usd": 5, "order_style": "maker", "order_timeout_s": 60, "max_entries_per_window": 1},
                 "exits": {"take_profit_pct": 0.10, "stop_fair_drop": 0.05, "sell_before_start_min": 5,
                           "hold_if_edge": True, "hold_min_edge": 0.02, "exit_slippage_ticks": 2, "exit_order_timeout_s": 5},
                 "risk": {"max_stake_usd": 10, "daily_loss_limit_usd": 30, "max_total_exposure_usd": 40,
                          "max_exposure_per_game_usd": 15, "max_exposure_per_league_usd": 40,
                          "max_consecutive_losses": 6, "min_wallet_balance_usd": 10},
                 "odds": {"min_books": 2, "max_quote_age_s": 99999, "sources": {}},
                 "research": {}}}


def make_module(tmp_path, starts_in_s=6 * 3600):
    eng = SimpleNamespace(db=DB(tmp_path / "s.db"), alerts=[])
    eng.alert = lambda *a, **k: eng.alerts.append(a)
    mod = SportsModule(eng, SC)
    start = time.gmtime(time.time() + starts_in_s)
    g = parse_event(gw_event(time.strftime("%Y-%m-%dT%H:%M:%SZ", start)), "nba")
    mod.games[g.event_slug] = g
    mod.market_index.update(g.markets)
    now = time.time()
    mod.odds_events["therundown:nba"] = [OddsEvent("therundown", "e1", "nba", "Atlanta Hawks", "Los Angeles Lakers",
                                                   g.starts_at, [
        q("draftkings", "moneyline", "Los Angeles Lakers", None, -150, now), q("draftkings", "moneyline", "Atlanta Hawks", None, 130, now),
        q("fanduel", "moneyline", "Los Angeles Lakers", None, -155, now), q("fanduel", "moneyline", "Atlanta Hawks", None, 135, now)])]

    async def no_research(*a, **k):
        from app.modules.sports.research import GameResearch
        return GameResearch()
    mod.research.game = no_research
    return eng, mod, g


def book(mod, slug, bid, ask, size=5000):
    from app.ingest.us_markets import us_book_to_store
    us_book_to_store(mod.books, slug, {"marketData": {"bids": [{"px": {"value": str(bid)}, "qty": str(size)}],
                                                      "offers": [{"px": {"value": str(ask)}, "qty": str(size)}]}},
                     time.time(), {})


def test_module_signal_requires_edge_beyond_buffer_and_fees(tmp_path):
    eng, mod, g = make_module(tmp_path)
    book(mod, "aec-nba-lal-atl", 0.55, 0.56)                    # Lakers fair ~59.5%: edge < 5% buffer
    asyncio.run(mod.evaluate())
    row = next(r for r in mod.board if r["bet_type"] == "moneyline")
    assert not mod.current_signals and any("buffer" in b for b in row["blocked_by"])
    book(mod, "aec-nba-lal-atl", 0.49, 0.50)                    # Lakers at 0.50: edge ~ +8% after fee & buffer
    asyncio.run(mod.evaluate())
    sig, = mod.current_signals
    assert sig.side == "Lakers" and sig.token.endswith(":long") and sig.bet_type == "moneyline"
    assert sig.group == "nba:nba-lal-atl" and 0.5 < sig.fair_prob < 0.62 and sig.edge > 0
    assert "Consensus 2 book" in sig.reasons[0].text


def test_module_blocks_stale_line_and_last_15_minutes(tmp_path):
    eng, mod, g = make_module(tmp_path)
    book(mod, "aec-nba-lal-atl", 0.49, 0.50)
    from app.modules.sports.research import GameResearch

    async def fresh_news(*a, **k):
        r = GameResearch()
        r.newest_news_ts = time.time() + 5                      # newer than every odds quote
        return r
    mod.research.game = fresh_news
    asyncio.run(mod.evaluate())
    assert not mod.current_signals and any("newer than the odds" in b for b in mod.board[0]["blocked_by"])
    eng2, mod2, g2 = make_module(tmp_path / "b", starts_in_s=10 * 60)
    book(mod2, "aec-nba-lal-atl", 0.49, 0.50)
    asyncio.run(mod2.evaluate())
    asyncio.run(asyncio.sleep(0))
    assert not mod2.current_signals
    assert any("Pinnacle" in b for b in mod2.board[0]["blocked_by"])


def test_exit_policy_hold_or_sell_at_start(tmp_path):
    eng, mod, g = make_module(tmp_path, starts_in_s=3 * 60)     # inside sell_before_start window
    mod.fairs["aec-nba-lal-atl"] = (None, 0.60, time.time())
    p = {"slug": "aec-nba-lal-atl", "token": "aec-nba-lal-atl:long", "p_model": 0.60}
    v = value_position(shares=10, cost_basis=5.0, bids=[(0.52, 1000)], fee_schedule={"rate": 0.095})
    assert mod.exit_decision(p, v, time.time()).trigger == "hold_to_resolution"   # 0.60 fair vs ~0.50 net
    mod.fairs["aec-nba-lal-atl"] = (None, 0.53, time.time())
    assert mod.exit_decision(p, v, time.time()).action == "SELL"
    eng2, mod2, g2 = make_module(tmp_path / "c")
    mod2.fairs["aec-nba-lal-atl"] = (None, 0.52, time.time())
    assert mod2.exit_decision({**p, "p_model": 0.60}, v, time.time()).trigger in ("stop_loss", "edge_gone")
    v_up = value_position(shares=10, cost_basis=5.0, bids=[(0.60, 1000)], fee_schedule={"rate": 0.095})
    mod2.fairs["aec-nba-lal-atl"] = (None, 0.70, time.time())
    assert mod2.exit_decision(p, v_up, time.time()).trigger == "take_profit"


def test_correlated_exposure_counts_whole_game(tmp_path):
    eng, mod, g = make_module(tmp_path)
    from app.modules.base import Signal
    sig = Signal("sports", "asc-nba-lal-atl-pos-1pt5", "x:long", "Lakers +1.5", 0.6, 1, 0.08,
                 group="nba:nba-lal-atl", league="nba", stake_usd=5)
    active = [{"status": "open", "group_key": "nba:nba-lal-atl", "league": "nba", "shares": 20, "exit_shares": 0,
               "cost": 9.5, "entry_fee": 0}]
    active[0]["cost"] = 10.5                                    # 10.5 + 5 > 15 per-game cap
    assert any("game exposure" in b for b in mod.entry_blockers(sig, active, time.time()))
    active[0]["cost"] = 9.0                                     # 9 + 5 = 14 <= 15
    assert not mod.entry_blockers(sig, active, time.time())
    active[0]["group_key"] = "nba:other-game"                   # different game: only league cap applies
    active[0]["cost"] = 36.0
    assert any("nba exposure" in b for b in mod.entry_blockers(sig, active, time.time()))


def test_sports_paper_trade_end_to_end_with_clv(tmp_path):
    eng, mod, g = make_module(tmp_path)
    store = TradeStore(eng.db, "paper", "polymarket_us", "sports")
    mod.attach(store)
    eng.durations = []
    broker = PaperBroker(mod.books.get, mod.books.trades_since, lambda t: {"rate": 0.095, "exponent": 1})
    t = AutoTrader(eng, {"trading": {}}, broker, store, module=mod, exec_conf=SC["sports"], min_paper_trades=200)

    async def run():
        book(mod, "aec-nba-lal-atl", 0.49, 0.51)
        await mod.evaluate()
        assert mod.current_signals
        await t.tick(time.time())
        p, = store.active_positions()
        assert p["status"] == "opening" and p["entry_price"] == 0.50 and p["group_key"] == "nba:nba-lal-atl"
        book(mod, "aec-nba-lal-atl", 0.49, 0.50)                # ask comes to our bid -> maker fill
        await t.tick(time.time())
        p = store.position(p["id"])
        assert p["status"] == "open" and p["entry_fee"] == 0 and p["module"] == "sports"
        # game starts: closing line captured from our own history -> CLV on the position
        kickoff = time.time()                                   # lines were recorded before kickoff
        mod.games["nba-lal-atl"].starts_at = kickoff
        mod.market_index["aec-nba-lal-atl"].starts_at = kickoff
        mod._capture_closing(mod.games["nba-lal-atl"], kickoff + 1)
        p = store.position(p["id"])
        assert p["closing_fair"] is not None and p["clv"] == pytest.approx(p["closing_fair"] - 0.50)
        await t.tick(time.time())                               # hold phase -> resolving
        assert store.position(p["id"])["status"] == "resolving"
        mod.settled["aec-nba-lal-atl"] = 1.0                    # Lakers won
        await t.tick(time.time())
        p = store.position(p["id"])
        assert p["status"] == "closed" and p["pnl"] == pytest.approx(p["shares"] * 1.0 - p["cost"])
    asyncio.run(run())
