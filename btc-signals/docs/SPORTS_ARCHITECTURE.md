# Multi-Market Architecture & Sports Module: Proposal

Status: **proposal, awaiting approval** (no code yet). Researched 2026-11-09 against the live
Polymarket US gateway, its docs (docs.polymarket.us), and the data providers below.

---

## 1. Findings that shape the design

### 1.1 Venue: Polymarket US, not the Gamma API
You're in Virginia. The Gamma API belongs to polymarket.com, which blocks US users, so the
sports module trades **Polymarket US**. The same data is on its public gateway
(`gateway.polymarket.us`), with no key needed for market data:

| Need | Polymarket US endpoint (verified) |
|---|---|
| Leagues | `GET /v1/sports`: 400+ league slugs (nfl, nba, nhl, mlb, cfb, cbb, epl, ucl, mls, …) |
| Games in a league | `GET /v2/leagues/{slug}/events`: one moneyline per game in the listing |
| All markets for a game | `GET /v1/events/slug/{slug}`: e.g. Lakers–Hawks had **642 markets**: 1 moneyline, 117 spreads, 188 totals, 336 player props |
| Book / BBO | `GET /v1/markets/{slug}/book` |
| Price history | `GET /v1/price-history?symbol={slug}&fixedInterval=INTERVAL_1D&fidelity=5` (5-min bars) |
| Odds-provider ID mapping | `GET /v1/partners/opticodds/events/{fixtureId}`: native OpticOdds mapping |

Typed fields to use, not slugs: `sportsMarketType` (e.g. `basketball_team_full_game_spread`),
`sportsMarketTypeV2` (MONEYLINE / SPREAD / TOTAL / PROP), `line`, `outcomes`, `marketSides[].long`.

### 1.2 Sports vs crypto on Polymarket US
| | Crypto Up/Down | Sports |
|---|---|---|
| Taker fee | Θ·C·p(1−p), Θ = 0.095 (tier 1) | **same**. Exception: Table Tennis Θ = 0.10. First 7 days Θ = 0.0695 |
| Maker fee | 0 | 0 |
| Tick / min size | 0.01 / 0.01 contracts | **same** |
| Order API | `/v1/orders`, maker-only flag, GTC | **same**, so the existing Polymarket US broker works unchanged |
| Structure | one long instrument (Up) | one long instrument per market. Moneyline: long = first-listed team. Soccer: 3 separate Yes/No markets (Home / Draw / Away) |
| Trading during the event | n/a | **markets stay open in-game**. The module must stop entries at `event start` unless live betting is enabled |

### 1.3 Settlement rules that change the maths (Sports FAQ + market text)
- **Overtime counts** for winner, spread and total markets. **Soccer is 90 minutes + stoppage**
  (draw is an outcome).
- **NFL winner tie settles at $0.50.**
- **Voids are NOT refunded at your entry price.** Postponement beyond 2 days, cancellation, a
  player not playing (props) and no-contests all settle at the **last fair market price at the
  time of the announcement**. So a postponement mid-position settles near where the market was,
  not at your cost.
- Spreads and totals use half points (no pushes). Each market's text states its own terms
  (e.g. "Overtime is included if played. If … rescheduled … not within two days … last fair
  market price"). The parser will **skip any market whose text doesn't match a known template.**
- Market Integrity policy: trading on material non-public information is prohibited. The module
  uses only public sources.

### 1.4 Liquidity (live sample, NBA Lakers–Hawks)
| Market | Best bid / ask | Depth (top 3 levels) |
|---|---|---|
| Moneyline | 0.48 / 0.49 | ~330,000 contracts each side |
| Spread ±1.5 | 0.49 / 0.50 | 45,000–96,000 |
| Total 225.5 | 0.71 / 0.74 | ~10,000–13,000 |
| Player props | e.g. 0.97 / 0.99 | often 1–60 contracts |

So moneylines and core spreads/totals are tradeable; most props aren't.

### 1.5 An honest expectation
Moneyline prices this deep are arbitraged against sportsbooks constantly. Against a de-vigged
sharp line, **edges big enough to clear a 0.095 taker exit fee (≈2.4¢ at 50¢, ~4.75% of stake)
plus a 5% buffer will be rare.** Most of the time the module will correctly say NO TRADE. Where
edge can exist:
1. Polymarket lagging a sharp line move.
2. Fresh injury or lineup news before Polymarket reprices.
3. Thin alt-lines priced off the main line.

Our data is delayed: The Odds API's Pinnacle prices are scraped "from public website which
may incur a delay", and ESPN injuries lag official sources. So **the measured CLV in paper
trading is the deciding number.**

---

## 2. Refactored architecture

```
app/
  core/                         SHARED by every module (moved from today's code, behaviour unchanged)
    execution.py                Broker interface: PaperBroker, UsLiveBroker, LiveBroker (intl)
    executor.py                 turns a Signal into orders; position lifecycle (opening/open/closing/resolving)
    risk.py                     RiskManager per module + GlobalRisk (combined daily loss, total exposure)
    exposure.py                 correlation groups: game-level and league-level exposure aggregation
    killswitch.py               global STOP (all modules) + per-module pause
    gate.py                     paper→live gate per module (crypto 300, sports 200 trades, per venue)
    store.py                    positions/orders/events tables, now with module, league, game_id, bet_type, group
    notify.py, feeds/, db.py    unchanged
  modules/
    base.py                     StrategyModule interface (below)
    crypto_updown/              today's engine logic, wrapped; must pass the existing 116 tests unchanged
    sports/
      discovery.py              Polymarket US leagues → events → markets; typed-field classification
      rules.py                  parse market text → RuleSpec (OT, voids, tie handling); skip unknown/ambiguous
      liquidity.py              min depth / max spread / min volume filters (per league)
      odds/
        provider.py             OddsProvider interface + cache + credit budgeting
        theoddsapi.py           The Odds API implementation (Pinnacle > Betfair Exchange > LowVig/BetOnline > consensus)
        devig.py                multiplicative, power and Shin de-vig; 2-way and 3-way
        linehistory.py          stores every snapshot → movement detection and closing line for CLV
      matching.py               Polymarket event ↔ odds event (league, start time ±15 min, team-name aliases)
      research/
        injuries.py             ESPN injuries (all leagues), re-checked 60/15/5 min before start
        lineups.py              MLB probable pitchers + lineups (MLB Stats API); NHL goalies (api-web, when posted)
        stats.py                rest days, back-to-backs, travel distance, home/away splits, recent form (ESPN schedules/standings)
        news.py                 ESPN news per league; impact scoring (player importance × status change × recency)
        weather.py              Open-Meteo at the venue for outdoor games (ESPN venue `indoor` flag)
      fair.py                   baseline = de-vigged sharp line; capped, dated, sourced adjustments; confidence
      exits.py                  TP at fair or +X%, stop when fair moves against us, hold-to-resolution option, pre-game only
  api/ + web/                   tabs: Crypto | Sports | Combined P&L; sports board; research cards
backtest/
  crypto/ (existing)            unchanged
  sports/                       Polymarket US price history + Odds API historical closing lines → CLV, ROI, calibration
```

### 2.1 Module interface (every module outputs the same thing)
```python
@dataclass
class Signal:
    module: str                  # "crypto_updown" | "sports"
    market_id: str               # venue slug
    token: str                   # "<slug>:long" | "<slug>:short"
    side: str                    # human label, e.g. "UP", "Lakers", "Over 225.5"
    fair_prob: float             # model fair probability for `side`
    confidence: float            # 0..1: data freshness, source agreement, rule certainty
    edge: float                  # fair - executable price - fees - buffer
    reasons: list[Reason]        # [(text, source, value, timestamp)], top 3 shown on the dashboard
    group: str                   # correlation group, e.g. "nba:lal-atl-2026-11-09"
    league: str | None
    bet_type: str                # updown | moneyline | spread | total | prop
    starts_at: float | None      # no entries after this unless live betting enabled
    expires_at: float

class StrategyModule(Protocol):
    name: str
    async def start(self) -> None: ...
    async def evaluate(self, now: float) -> list[Signal]: ...                 # cadence chosen by module
    def fair_value(self, position) -> tuple[float | None, bool]: ...          # for exits (value, model_ok)
    def exit_policy(self, position, valuation, now) -> ExitDecision: ...
    def snapshot(self) -> dict: ...                                           # module tab on the dashboard
```
The executor, risk, kill switch, paper/live, audit log and alerts stay shared. A third module
(politics, weather, …) only implements this interface.

### 2.2 Risk (per module + global)
```yaml
risk:
  global:  {daily_loss_limit_usd: 40, max_total_exposure_usd: 60}
  crypto_updown: {max_stake_usd: 5, daily_loss_limit_usd: 25, max_exposure_usd: 15, max_consecutive_losses: 5}
  sports:
    max_stake_usd: 10
    daily_loss_limit_usd: 30
    max_exposure_per_game_usd: 15     # all positions in one game = one correlated exposure
    max_exposure_per_league_usd: 40
    max_consecutive_losses: 6
    buffer: {default: 0.05, nba: 0.05, nfl: 0.05, cbb: 0.07}    # per-league edge buffer
```
- **Correlation:** every position carries `group = league:event`. Exposure per game is the
  **sum of cost basis across all positions in that game**: moneyline, spread, total, props,
  either side. That's conservative: no netting credit for "hedges". League exposure is summed
  the same way.
- The global kill switch stops everything. Each module can also be paused by itself.

### 2.3 Sports fair probability
1. **Baseline:** de-vigged sharp line for the exact market. Pinnacle, else Betfair Exchange,
   else LowVig/BetOnline, else multi-book consensus. Each fallback lowers `confidence`.
   Default de-vig method is **power** (handles favourite–longshot bias better than
   multiplicative). Shin is optional for 3-way soccer.
2. **Exact-line rule:** v1 trades only Polymarket markets whose line equals a line the sharp
   book quotes (moneyline, main spread, main total). Alt-lines derived from a margin
   distribution are phase 2. Props are off by default: no sharp prop source on these plans, and
   the books are thin.
3. **Adjustments** apply only for information **timestamped after the sharp line's last move**,
   e.g. a starter ruled out 6 minutes ago while Pinnacle hasn't moved. Each adjustment:
   - comes from a per-sport table (e.g. NBA star out ≈ x points of spread → probability via a
     normal margin model), capped at a max per signal
   - is recorded as a `Reason` with its source URL and timestamp
   - expires when the sharp line moves, which means the market has absorbed it
4. **Line movement:** flag when the sharp line moved more than X since the last snapshot and
   Polymarket's mid hasn't moved correspondingly. That raises confidence for the side the
   sharp line moved toward.
5. **Weather** (outdoor NFL/MLB/soccer): wind and precipitation flags shown as reasons. A
   numeric adjustment only for totals, small and capped.

### 2.4 Sports trading rules
- Enter only if `edge > fees + spread + buffer(league)`. Maker-only entries, as in crypto.
- **No entries after `starts_at`** unless `sports.live_betting: true`. Resting orders are
  cancelled 2 minutes before start.
- Exits: take profit when the bid nets ≥ fair or ≥ +X%. Stop when new information lowers fair
  value by more than Y or the sharp line moves against us. Hold to resolution allowed when edge
  persists at kickoff (configurable). With live betting off, no in-game exits either: it's hold
  or exit before start.
- Rate limits: the Polymarket US gateway allows 25 req/s shared with the crypto module. Events
  are refreshed every 5 minutes, books only for shortlisted markets, and odds are polled on a
  credit budget (below).

### 2.5 Validation
- **Backtest:** Polymarket US 5-minute price history for resolved games, plus Odds API
  **historical** snapshots (closing line ≈ last snapshot before start), plus results (ESPN).
  Reports per league and bet type:
  - **CLV** (entry price vs de-vigged closing probability)
  - ROI after fees, win rate, calibration
- **Paper:** CLV is recorded live for every paper trade (closing line captured at start).
  Live needs **200 closed paper sports trades** on Polymarket US.
- Without a paid odds key, only Polymarket's own calibration can be backtested, not CLV.

### 2.6 Dashboard
- Tabs: **Crypto | Sports | Combined**. The STOP button and the PAPER/LIVE badge stay global.
- **Sports board:** upcoming games by league, showing Polymarket price vs fair, edge,
  confidence, start time, injuries flagged, and recent line moves.
- **Research card** per signal: top 3 reasons with source links and timestamps.
- **P&L and CLV** by league and by bet type. Combined equity curve across modules.

---

## 3. Data sources

| Purpose | Source | Cost | Why |
|---|---|---|---|
| Markets, books, history, settlement | Polymarket US gateway | free, no key | the venue you trade on |
| **Sharp odds + line history** | **The Odds API** | **paid needed: $30/mo** (20K credits); $59/mo (100K) for 10-min polling + history | includes Pinnacle (EU region, scraped with a delay), Betfair Exchange, LowVig, BetOnline. Free tier is 500 credits/month, only enough to test |
| Fresher Pinnacle (optional upgrade) | pinnapi ($99–229/mo), SportsGameOdds ($99–499/mo), OpticOdds (quote, "high hundreds"/mo) | paid | lower latency. Polymarket US natively maps OpticOdds fixture IDs |
| Injuries, scoreboards, team stats, news | ESPN site API (`site.api.espn.com`) | free, no key | all leagues; verified injuries with timestamps and notes, venue `indoor` flag. **Unofficial and undocumented**, so it can change without notice. Cached, failures degrade gracefully |
| MLB probable pitchers + lineups | MLB Stats API (`statsapi.mlb.com`) | free, official | confirmed lineups and starters |
| NHL schedule, stats, goalies | NHL API (`api-web.nhle.com`) | free, official | starting goalies only when posted, close to game time |
| Weather | Open-Meteo | free, no key (non-commercial) | hourly wind, temperature, precipitation at venue coordinates |
| Soccer fixtures (optional) | football-data.org | free tier (10 req/min, key) | EPL/UCL standings and fixtures if ESPN coverage is thin |
| Not used | stats.nba.com (blocks cloud/heavy use), NBA injury PDFs (403 from servers; ESPN mirrors them), RotoWire/SportsDataIO (paid lineups/news, a possible later upgrade) | – | – |

Credit budgeting for The Odds API (1 credit = 1 market × 1 region per call; history is 10×):
5 leagues × 3 markets × 1 region = 15 credits per sweep. A 30-minute sweep during game days is
about 720/day, which fits the $30 plan. A 10-minute sweep plus closing-line capture fits the
$59 plan.

---

## 4. Build order
1. **Refactor** into `core/` + `modules/crypto_updown` with no behaviour change. All existing
   tests must pass unchanged. Store migration adds module, league, game, group and bet type.
   Per-module risk and dashboard tabs.
2. **Sports discovery + rules parser + liquidity filter**, giving a sports board with
   Polymarket prices (no odds key needed).
3. **Odds provider + de-vig + matching + line history**, giving fair value and edge on the
   board (needs the key).
4. **Research layers** (injuries, lineups, stats, news, weather) as dated, capped adjustments
   with reasons.
5. **Sports execution in paper mode** through the shared executor, with per-game and
   per-league exposure and CLV capture.
6. **Sports backtest** and CLV/ROI/calibration reports.
