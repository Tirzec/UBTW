# BTC Up/Down Signals

A local signal app for Polymarket's **"Bitcoin Up or Down"** 5-minute and 15-minute markets.
For each live window it shows **UP**, **DOWN** or **NO TRADE**, explains why, and gives
**SELL / HOLD** guidance for positions you hold.

**It never places trades.** It has no wallet, no private key and no order code. You execute manually.

> ⚠️ The default model has **not** shown a statistically significant edge in backtests
> (see [Validation status](#validation-status)). Run paper trading for a few hundred windows and
> read the report before risking money.

---

## How these markets actually resolve (verified 2026-11-09)

| | |
|---|---|
| Resolution | Chainlink BTC/USD **60-second TWAP** stream (`btc-usd-twap-60s`), for **both 5m and 15m** markets (`cryptoMarketConfig.twapLookbackSeconds = 60`). There is no 30s TWAP. |
| Price to beat | The same 60s TWAP **at the window start**. It equals the previous window's final price. |
| UP wins if | TWAP at window end **>=** price to beat. |
| Proof | Gamma `eventMetadata.priceToBeat/finalPrice` matched the actual payout in **137/137** recent windows. The app's own Chainlink capture matched Gamma's `priceToBeat` to the last digit. |
| ⚠️ Don't use | polymarket.com's `/api/crypto/crypto-price` open/close values. They are the Chainlink **spot** at the boundary, and they named the wrong winner in **15/138** windows. The dashboard shows that value greyed out next to the real price to beat for comparison. |
| Fees | Takers only: `shares × 0.07 × p × (1 − p)`, read per market from `feeSchedule`. Selling early pays it again. |

Each market's rules text and config are parsed and checked at runtime (`app/rules.py`).
Any market that differs (other lookback, other source, other comparator, TWAP disabled) shows a
**RULES MISMATCH** banner and is forced to NO TRADE.

---

## Setup

Runs on **Windows, macOS and Linux** with Python 3.9+.
**Windows: follow [docs/WINDOWS.md](docs/WINDOWS.md)** (PowerShell setup, background running,
safe stop, clock sync, power settings). In short:

```powershell
cd btc-signals
.\windows\setup.bat          # venv + dependencies + .env + tests + clock check
.\windows\start-bot.bat      # runs in the background, opens http://localhost:8000
.\windows\stop-bot.bat       # kill switch (cancel all orders, halt), then stop the process
```

macOS / Linux:

```bash
cd btc-signals
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional: Polymarket API keys for the PolyBolt feed
python -m pytest                # about 2 s
python -m app.main              # opens http://localhost:8000
python -m app.clock             # is the computer's clock right? (the 5-minute windows need it)
```

The app keeps the computer from idle-sleeping while it runs (`server.keep_awake`, or
`--no-keep-awake`); no `caffeinate` needed.

Click **Enable alerts** once in the browser to allow sound and desktop notifications.

**"This site can't provide a secure connection" / "can't establish a secure connection"?**
Something on your computer (a school or work security profile, a VPN, or a browser setting) is
forcing https even for localhost. Start the bot with `--https` instead
(`python -m app.main --https`, or `.\windows\start-bot.ps1 -Https`).

Then open **https://localhost:8000**. The first time, the browser warns that the certificate isn't
trusted. It's a certificate the app just made for your own computer, so it's safe to continue:
- **Chrome / Edge:** Advanced → Continue to localhost
- **Safari:** Show Details → visit this website

Set `server.https: true` in `config.yaml` to make this the default.

Start the app **before** a window begins. The price to beat comes from Chainlink at the boundary,
and Gamma only publishes it after the window closes. Windows already running at startup show
"price to beat unknown" (NO TRADE) unless the previous window's official final price is out.

### Network hosts used
Polymarket US: `gateway.polymarket.us`, `api.polymarket.us`.
Polymarket (intl): `gamma-api.polymarket.com`, `clob.polymarket.com`, `ws-subscriptions-clob.polymarket.com`,
`ws-live-data.polymarket.com` (RTDS), `ws-live-v2.polymarket.com` (PolyBolt, optional),
`data-api.polymarket.com` (backtest), `polymarket.com` (display-only cross-check).
Spot: `ws-feed.exchange.coinbase.com`, `ws.kraken.com`. Derivatives: `api.hyperliquid.xyz`.
News: CoinDesk, Cointelegraph, Decrypt, The Block RSS. Calendar: `nfs.faireconomy.media`.
Phone alerts: `ntfy.sh` or `api.telegram.org`.
Backtest bars: `data-api.binance.vision`. Binance's trading API is geo-blocked in the US and is off by default.

---

## Exchanges: Polymarket US (default) or polymarket.com

Set `venue:` in `config.yaml`.

| | `polymarket_us` (default) | `polymarket_intl` |
|---|---|---|
| Who can use it | US residents, 18+ (19 in AL/NE, 21 in MS), eligible states (not Nevada) | non-US only: blocks US IPs |
| Regulator | CFTC-regulated exchange | none (crypto, on-chain) |
| BTC Up/Down windows | 5m, 15m (also 1h, 1d) | 5m, 15m |
| Settles on | CF Benchmarks **BRTI**: average of 60 one-second prices before open/close; a flat window is **Up** | Chainlink 60s TWAP |
| Price to beat | published by the exchange (`assetPriceTerms.priceToBeat`) | captured by the app at window start |
| Taker fee | `0.095 × p × (1−p)` per contract (0.0695 in your first 7 days), read from each market's `feeCoefficient` | `0.07 × p × (1−p)` |
| Maker fee | 0 (maker-only orders supported) | 0 |
| Funding | debit card, ACH, Apple Pay, PayPal, Venmo, USDC | USDC/pUSD |
| Python for LIVE | 3.9+ (requests signed with PyNaCl) | 3.11+ (official SDK) |

How the Polymarket US connection works:
- Markets are found by slug (`cpc-btc-updown-5m-YYYY-MM-DD-HHMMz`).
- Order books are polled once a second from the public gateway (no key needed).
- The live BRTI price comes from the public asset-price API.
- Only the "Up" contract trades. A Down position is the short side, so a Down order at x is
  sent as `BUY_SHORT` at 1 − x. The app handles this for you.

Known limitations on Polymarket US:
- The public 1-second BRTI candles track the official settlement samples closely (within about
  $1–10), but not exactly. The bot uses the **official published price to beat**, and BRTI only
  for its estimate.
- A new 15m window's order book can take about 2 minutes to appear. The bot shows "order book
  stale" (NO TRADE) until it does.
- There's no exchange-side dead-man switch. If the app crashes, a resting $5 entry order can
  stay open until you restart. On startup the bot cancels all open orders, or run
  `python -m app.trader stop`.

### Going live on Polymarket US
1. Install the **Polymarket US** app, sign up, and pass identity verification (legal name,
   address, date of birth, SSN; sometimes a photo ID). Fund it with only what you can afford to lose.
2. Create an API key at **https://polymarket.us/developer**, signing in the same way as in the
   app. Copy the **secret key**; it's shown only once.
3. Put both in `btc-signals/.env` (never anywhere else):
   ```
   POLYMARKET_KEY_ID=...
   POLYMARKET_SECRET_KEY=...
   ```
   `.env` is in `.gitignore`, and both values are masked in every log. **The app refuses to start
   if `.env` (or any `.env.*`, `*.pem`, `*.key`) is tracked by git**, and tells you how to untrack it.
4. Run the read-only **preflight**: `python -m app.preflight --expect-balance <what the app shows>`
   (Windows: `.\windows\preflight.bat --expect-balance 50`). It checks: secrets not in git, API
   key, balance, open orders/positions, clock, crypto + sports markets and books, price feeds, risk
   limits, and paper trades (300 crypto / 200 sports). It only sends signed GET requests.
5. **Test order** (`python -m app.test_order`, Windows `.\windows\test-order.bat`): one
   minimum-size maker BUY at 1c, confirmed open, canceled, cancel confirmed (costs nothing). With
   `--fill` (and a typed phrase) it then buys the minimum at the ask and sells it straight back,
   reporting actual fees and slippage vs predicted. `--qty 1` gives a fee big enough to measure.
   `--kill-test` places a 1c order and fires the kill switch's cancel-all, confirming it's gone.
   `python -m app.trader fills` shows your real fills from the exchange's trade records.
6. Let **paper mode run for 300+ trades on Polymarket US** (`python -m app.trader stats`).
   Paper trades on the other exchange don't count.
**Live safety (always on in LIVE mode):**
- **Phase 1 hard caps** (in code, `app/trading/live_phase.py`; config can only lower them): stake <= $1,
  daily loss <= $10 across modules, <= 10 live trades per day.
- **Reconciliation every 60 s**: positions (`/v1/portfolio/positions`) and open orders vs. the bot's
  records. A mismatch on two checks in a row halts everything (orders canceled, alert sent). At startup
  positions are loaded from the exchange first; any mismatch halts before the first order.
- **No duplicate orders**: the exchange has no client order ID, so after a timeout or dropped connection
  the bot looks for the order (open orders, then your trade history) instead of re-sending it.
  Partial fills keep the filled part; immediate orders never rest; 429 rate limits pause briefly.
- `python -m app.trader reconcile` runs the comparison by hand (read-only).

**Entry and exit rules (fair-value strategy, 2026-11-10).** Only sides priced 55c-93c are bought (the model
overestimates cheap sides: see docs/FAIR_VALUE_RESULTS.md); one entry per settlement time (5m and 15m windows that
overlap count as one); 30-minute warm-up after every start (the full volatility look-back; warm-up blocks
entries only, never forces an exit). Exits (rules v2, 2026-11-10): resting SELL at max(entry + 25c, fair value + 3c),
capped at 95c and moved as fair value changes; otherwise hold toward resolution while the model's probability is
above the bid. Early sells only when the bid (after fees) is above fair value, at a 93c+ bid (lock-in, only in
profit), or late in the window; true probability < 30% sells at the bid. (The old +6c quick exit and entry + 10c
resting sell are gone.)

**Fast entries (rules v2).** Entries are checked on every order-book update (WebSocket, or poll fallback), not on
a timer; everything that only changes once a second (fair value, price to beat, thresholds, fees, stake) is
precomputed. With 8+ points of edge the buy goes in AT the best ask (fills now); smaller edges quote one tick
under. An unfilled entry is cancelled after 3 s and re-quoted on the next update if the edge is still there.
Book update -> order time is logged; a median over 300 ms alerts your phone. Every signal and why it was or
wasn't taken goes to `bot_signals`. `python -m app.trader execution --live` (Windows: `.\windows\execution.bat`)
shows entry timings, unfilled cancels, missed signal windows, profit left on the table by early sales, and old vs
new rules; after 50 trades on the new rules the bot sends that comparison to your phone once.
`python -m app.trader entries --live` (Windows: `.\windows\entries.bat`) explains every entry below 30c and
compares results below 30c vs above 50c.

**Fast exits (fair-value strategy).** A resting SELL for the whole position (see above) is placed the moment a
position opens (fills with no reaction delay); bid >= 93c sells immediately unless the averaging window
has started and fair > 99%; a profitable position with < 45 s (5m) / 90 s (15m) left sells at a bid >= 85c.
Open positions are checked every 250 ms and their books polled ~4x a second; with API keys, books stream
over the Markets WebSocket. Every position keeps a price/decision trace (`bot_ticks`), and each sell logs
price -> order latency (alert if the median exceeds 1 s). `python -m app.trader exits --live`
(Windows: `.\windows\exits.bat`) shows, for positions that reached a 90c+ bid, when it crossed, when the
bot sold, and why.

**Monitoring (free: ntfy or Telegram, set in `.env`).** In LIVE mode these always reach your phone:
every fill (buy and sell, with fee), daily loss limit / phase trade cap reached, reconciliation
mismatch, kill switch, errors (API errors, crashed background tasks). A **daily summary** is pushed
at `notifications.daily_summary_at` (default 23:55): trades, P&L after fees, win rate, average win vs
loss, fees, and CLV for sports. Any day, any time: `python -m app.trader summary [--live] [--date ...] [--send]`
(Windows: `.\windows\summary.bat`).

7. In `config.yaml` set `trading.mode: live` and `trading.live_trading_enabled: true`, then start
   the app. It checks that your key works (shows your buying power), prints your paper stats and
   risk limits, and asks you to type `I UNDERSTAND THIS TRADES REAL MONEY`.

---

## Strategy modules: crypto + sports

The auto-trader runs **strategy modules** that share one executor:
- order lifecycle and paper/live brokers
- risk controls and the kill switch
- the audit log, alerts and the dashboard

Every module outputs the same signal: market, side, fair probability, confidence, edge, and
the reasons with their sources.

| Module | What it trades | Status |
|---|---|---|
| `crypto_updown` | BTC 5m/15m Up/Down | unchanged strategy (see below) |
| `sports` | Polymarket US game markets: **moneyline, main spread, main total** (no props) | new, **paper** until 200 paper trades |

Add a category by implementing `app/modules/base.py:StrategyModule`.

Shared limits across all modules live under `risk_global` (combined daily loss $40, total
exposure $60). The STOP button and `python -m app.trader stop` halt every module. The dashboard
has **Crypto | Sports | Combined** tabs.

### Sports module (`sports:` in config.yaml)

**Leagues:** NFL, NBA, NHL and EPL are on. MLB, college football and basketball, MLS and the
Champions League are in the config, off. Polymarket US had no EPL games listed when this was
built, and soccer markets are only traded if their rules match a known template.

**Fair value = free sportsbook consensus.** DraftKings, FanDuel and BetMGM, de-vigged (power
method) and averaged. A book counts only if it quotes **both sides at exactly the same line** as
the Polymarket market. For each book, the newest quote from any source is used.

| Source | Free tier (checked in each provider's docs) | Used for | Key in `.env` |
|---|---|---|---|
| TheRundown | 20,000 data points/day **and** 200,000/month; 1 req/s; 5-min delay | DraftKings, FanDuel, BetMGM odds | `THERUNDOWN_API_KEY` |
| SharpAPI | 12 req/min, no monthly cap; 60-s delay | fresher DraftKings/FanDuel | `SHARPAPI_KEY` |
| The Odds API | 500 credits/month (event list free; 1 game × 1 market = 1 credit) | **Pinnacle check only**: within 60 min of start, only for games already showing an edge | `THE_ODDS_API_KEY` |
| ESPN (unofficial) | free | injuries, news, scoreboards (venue/indoor, back-to-backs) | – |
| Open-Meteo | free | weather at outdoor venues | – |

**Quotas are hard-capped.** Every source keeps a persistent ledger and reads the provider's own
"remaining" header. It stops at `budget.reserve` below the cap and never exceeds it, even across
restarts. Usage is shown on the Sports tab. TheRundown is refreshed on a schedule:
- every 3 h normally
- every 30 min within 2 h of a game
- a final closing snapshot

**Trading rules:**
- **Edge must beat fees + spread + 7%** (per-league buffer in config). The edge is measured
  against the live ask, including Polymarket US's taker fee (0.095 × p × (1−p)). Entries are
  maker-only limit orders.
- **No entries in the last 15 minutes** before start unless the Pinnacle check agrees within 3
  points. Resting orders are cancelled 2 minutes before start. No in-game trading
  (`live_betting: false`).
- **Fresh news blocks the game.** If an injury or news item is newer than the newest odds
  quote, the game is blocked until the books update. Research never moves the fair price by
  itself; it explains, flags and blocks.
- **Exits:**
  - take profit at +10%, or when the bid reaches fair value
  - stop when fair value falls 5 points below its level at entry
  - at kickoff: **hold to the result if the edge still holds, otherwise sell before the start**
- **Liquidity filter:** spread ≤ 3¢ and at least 200 contracts at the best price.
- **Risk (sports):** max $10 per trade, $30 daily loss, **$15 per game (every position in a game
  counts as one exposure)**, $40 per league, pause after 6 losses in a row.
- **Settlement:** Polymarket US's official settlement. Ties (e.g. NFL) pay $0.50. Postponements
  beyond 2 days settle at the "last fair market price", **not refunded at your entry**.

**Validation without paid history.** The app records every consensus line it sees
(`sports_lines`), the **closing line** for every market (`sports_closing`), and the official
settlement. Every trade gets **CLV = closing fair − entry price**. Run:
```bash
python -m backtest sports            # CLV, ROI, win rate, calibration by league and bet type
```
Let paper trading run long enough to build this dataset. Live sports trading needs
`sports.live_enabled: true`, the normal live checks, and **200 closed paper sports trades**.

Without any odds keys, the Sports tab still shows games, Polymarket prices and research, but
computes no fair value and never trades.

---

## Auto-trader (PAPER by default)

The app can trade by itself. It starts in **PAPER** mode: real signals, real Polymarket order
books, simulated fills, no money. Every order, fill, cancel and error is written to SQLite
(`bot_orders`, `bot_events`, `bot_positions` in `data/signals.db`) and to `data/trader.log`.

### Fees (confirmed from Polymarket's docs, Nov 2026)
Crypto Up/Down markets charge **takers** `shares × 0.07 × p × (1 − p)` (max 1.75¢/share at 50¢).
**Makers pay 0.** The rate comes from each market's `feeSchedule`, so a change is picked up
automatically.
- **Entries** are **post-only limit orders**. They rest on the book, so they pay **no fee and no
  spread**, but they may not fill. Unfilled orders are cancelled after `order_timeout_s` (8s).
- **Exits** are limit sells at the bid minus 2 ticks, so they fill immediately and **pay the taker fee**.
- Every P&L number (take profit, stop loss, dashboard, stats) is net of the fees actually paid.

### What it does
| | Default | Setting (`trading:` in config.yaml) |
|---|---|---|
| Enter only on a signal (edge > fees + spread + buffer) | existing signal logic | `signal.buffer` |
| No entries late in the window | last 60s (5m), last 120s (15m) | `entry.no_entry_last_s_*` |
| ...unless the edge is very high | edge ≥ 0.12 | `entry.late_entry_min_edge` |
| One position per window at a time | max 2 entries per window | `entry.max_entries_per_window` |
| Take profit | +8% after fees, or the bid reaches the model's fair value | `exits.take_profit_pct` |
| Stop loss | −10% after fees, or the model's P(win) drops below 50% | `exits.stop_loss_pct`, `exits.flip_threshold` |
| Time exit | 20s (5m) / 30s (15m) before close: hold only if E[payout] beats selling by 2¢/share | `exits.time_exit_s_*`, `exits.hold_min_ev` |
| Exit checks | every 1s, valuing only what the current bids can absorb | `exits.check_interval_s` |

### Risk controls
| Control | Default | What happens |
|---|---|---|
| Max stake per trade | $5 | hard cap |
| Daily loss limit | $25 | no new trades until local midnight |
| Consecutive losses | 5 | pause and alert; you press Resume |
| Max total exposure | $15 | new entries blocked |
| Degraded data | – | no new entries while degraded; open positions use price-based exits only |
| API errors | 3 in 5 min | pause and alert |
| Wallet balance | below $10 | pause and alert |
| **Kill switch** | – | the big **STOP** button, or `python -m app.trader stop`: cancels every open order and halts all trading. Open positions are left for you to manage. Survives restarts. |

Command line:
```bash
python -m app.trader stop      # kill switch (works even while the app is running)
python -m app.trader resume
python -m app.trader status
python -m app.trader stats     # paper and live results
```

### LIVE mode (real money)
LIVE refuses to start unless **all** of these hold:
1. `trading.mode: live` **and** `trading.live_trading_enabled: true` in config.yaml
2. At least **300 closed paper trades**. Your paper stats are printed for you to review.
3. **Python 3.11+** with `pip install "polymarket-client>=0.12.0"`, the official SDK, which needs 3.11.
   Python 3.9/3.10 can only run paper mode on polymarket.com; install Python 3.12 from python.org for live.
4. `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_WALLET_ADDRESS` in `.env`
5. **Polymarket's geoblock check passes.** The **United States is blocked** (close-only) on
   polymarket.com and its API. From the US, LIVE mode will refuse to start. Do not use a VPN to
   get around this: it breaks Polymarket's terms and they can freeze your funds.
6. You type `I UNDERSTAND THIS TRADES REAL MONEY` in the terminal.

In LIVE mode the bot sends Polymarket **order heartbeats** every 5s. If the app crashes or loses
its connection, Polymarket automatically cancels its open orders after 10s.

**Security:** keys live only in `.env`, which is git-ignored, and are redacted from all logs.
**Use a separate wallet funded only with what you're willing to lose.** Never put your main
wallet's key in this app.

---

## Phone alerts (works anywhere, phone locked)

The app runs on your computer, and alerts are pushed to your phone over the internet through
[ntfy](https://ntfy.sh) (free, no account) or Telegram.

**ntfy setup (2 minutes):**
1. Install **ntfy** from the App Store or Google Play.
2. In the app, tap **+** and subscribe to a topic name only you know, e.g. `btc-signals-k3x9q7m2v8`.
   Anyone who knows the name can read your alerts, so make it long and random.
3. On your computer, put the same name in `btc-signals/.env`:
   ```
   NTFY_TOPIC=btc-signals-k3x9q7m2v8
   ```
4. Send a test: `python -m app.notify`. Your phone should buzz.
5. Start the app as usual (`python -m app.main`). The Feeds panel shows "phone alerts: ntfy (N sent)".

**What you get:**

| Alert | Example |
|---|---|
| New signal | **5m: BUY UP @ ≤0.55**: model 62% vs ask 0.55, edge +2.3%, 240s left, the top reasons, and the market link |
| Advice change on a position you added | **Position #3 5m UP: SELL @ ~0.81**, with the reason (edge gone, stop, or lock profit) |
| Data problem | **DEGRADED DATA - signals off**, and "signals back on" when it recovers |
| Market rules changed | **RULES MISMATCH - no trading** |

Choose which alerts are pushed under `notifications.kinds` in `config.yaml`. Add `settled` to
also get window results.

**Telegram instead:**
1. Create a bot with @BotFather and send your bot any message.
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

If both ntfy and Telegram are set, alerts go to both.

**Away from your computer:** to act on a SELL for a position you entered in the dashboard, sell in
the Polymarket app. To add a position or see the full dashboard from your phone, use Tailscale (free):
1. Install it on the computer and the phone, signed in to the same account.
2. Set `server.host: 0.0.0.0` in `config.yaml`.
3. On the phone, open `http://<computer-name>:8000`.

Never expose port 8000 to the internet; the dashboard has no login.
The computer must stay on and awake with the app running (turn off sleep).

---

## Price feeds: one interface, swappable from config

Every source implements `PriceFeed` (`app/feeds/base.py`) and emits `PriceTick`s. Reconnects,
backoff and health checks are shared. Pick and order sources in `config.yaml`. No code changes:

```yaml
feeds:
  reference:                     # Chainlink; first healthy one wins
    - type: polymarket_rtds      # legacy RTDS, no auth (Polymarket is retiring it)
    - type: polybolt             # replacement; needs POLY_API_KEY/SECRET/PASSPHRASE in .env
  spot:                          # first healthy one is primary
    - type: coinbase
    - type: kraken
      no_price_after_s: 45       # per-feed health overrides
```

Built-in types: `polymarket_rtds`, `polybolt`, `coinbase`, `kraken`, `binance`. To add another,
subclass `PriceFeed`, implement `stream(emit)`, and register it in `FEED_TYPES` (`app/feeds/sources.py`).

**Staleness warnings.** A feed is marked STALE, and the log prints
`PRICE FEED STALE: <feed> - <reason>`, when any of these happens:
- no message at all for `stale_after_s` (the feed stopped responding)
- no price for `no_price_after_s`
- the source timestamp lags local time by more than `max_lag_s`
- the price hasn't changed for `frozen_after_s`

The warning repeats every `warn_repeat_s`. Feed switches and recoveries are logged too, and the
dashboard's Feeds panel shows the reason.

**Degraded data → NO TRADE.** `app/feeds/quality.py` compares Chainlink with the Coinbase/Kraken
median every second. The dashboard shows a red **DEGRADED DATA** banner and every signal becomes
NO TRADE when:
- the Chainlink−spot gap exceeds `data_quality.max_gap_bps` for `gap_persist_s`
- every Chainlink feed is down, so the app falls back to its **Coinbase-based estimate**
  (spot 60s TWAP plus the last known basis)
- the exchanges disagree with each other by more than `max_spot_disagreement_bps`
- no spot feed is live

Open positions get a SELL advisory ("data unreliable"). Recovery needs `recover_after_s` of
clean data.

Polymarket quirks the RTDS client handles:
- one topic per connection (two subscriptions on one socket stop live updates)
- filter JSON without spaces (`{"symbol":"btc/usd"}`)
- the Chainlink snapshot arrives under topic `crypto_prices`

---

## How a signal is made

1. **Features** (`app/features.py`, pure functions shared with the backtester):
   - distance from the price to beat in σ of the *settlement TWAP*, with the part of the
     average already fixed in the final 60s taken into account
   - time left
   - 1m/3m/5m momentum
   - VWAP deviation
   - RSI(14, 1m)
   - 60s taker order-flow imbalance
   - realised-vol ratio
   - perp funding, ΔOI and book imbalance (Hyperliquid)
   - news sentiment
2. **Model** (`app/model.py`): logistic regression on top of the analytic prior
   `P0 = Φ((E[TWAP_end] − price_to_beat) / sd)`. Until you train one that beats the prior out of
   sample, it *is* the prior. Each feature's log-odds contribution becomes the "why" (top 3 drivers).
3. **Entry** (`app/signal.py`): for each side,
   `edge = P(side) − avg ask after walking the book for stake_usd − taker fee − buffer (0.04)`.
   Signal only if edge > 0, spread ≤ `max_spread`, the book is deep enough, and no gate blocks it.
   Gates:
   - rules mismatch
   - degraded data
   - unknown or estimated price to beat
   - warming up
   - too early or too late in the window
   - stale books
   - calendar blackout (USD high-impact releases −5/+10 min, overlapping the rest of the window)
   - news shock (a high-confidence headline in the last 5 min)
4. **Exit** (`app/exits.py`, every 2s). In this order:
   - **SELL (stop):** fair value drops more than `stop_threshold` below entry.
   - **SELL (edge gone):** the bid, net of fees, reaches fair value.
   - **SELL (lock profit):** the profit exceeds `take_profit` and little edge is left.
   - **HOLD** otherwise, with the remaining edge shown.

All thresholds are in `config.yaml`.

---

## Dashboard

- **5m and 15m cards:** countdown, price to beat (and its source), distance in σ, model vs market
  P(UP), best bid/ask, the signal in large text, the top 3 drivers, and the gates blocking a trade.
- **My positions:** enter side, entry price and shares for a trade you placed. You get live value,
  fair value and SELL/HOLD with the reason. An alert fires when the advice changes.
- **Paper stats:** win rate, average edge, EV per trade, total PnL, max drawdown, hold-only PnL,
  Brier score, a calibration chart and an equity curve.
- **Feeds, calendar, derivatives, news** (with each headline's label, confidence and the matched
  words), plus an alert log. Sound and desktop notifications fire on new signals, SELL/HOLD changes
  and data degradation.

---

## Validation (do this before trusting any signal)

### 1. Backtest

```bash
python -m backtest collect --days 3                # Gamma outcomes, v2 trade tapes, Binance 1s bars
python -m backtest run --prior-only --out reports/prior
python -m backtest train --train-frac 0.7          # fits on the first 70% of windows (walk-forward)
python -m backtest run --from-frac 0.7 --out reports/oos   # out-of-sample on the last 30%
open reports/oos/report.html
```

`collect` is resumable and rate-limited; rerun it to fill gaps. `train` refuses to save a model
that does not beat the prior out of sample (`--force` to override).

The replay (`backtest/replay.py`) uses the same feature, model, signal and exit code as live, with
**no look-ahead** (a unit test checks this). Historical order books don't exist, so fills are modelled:
- ask = last trade ± `half_spread` (default 1¢)
- fill at the worse of the price at the signal and the price `latency_s` later
- plus a size-based impact
- the trade only happens if that outcome traded at least the stake within ±30s
- the exact taker fee on entry and on any early exit

Binance USDT 1s bars are re-anchored to each window's real Chainlink price to beat.
Not replayed historically: derivatives, news and order-book imbalance (set to 0).

The report gives:
- win rate, average edge, EV per trade and per $, max drawdown and t-stat, both following the exit
  rules and holding to resolution
- Brier score and log-loss vs the market
- calibration charts (predicted vs actual frequency) for all decision points and for trades taken

Tweak with `--half-spread`, `--latency`, `--buffer`, `--stake`, `--no-exits`.

### 2. Paper trading

Always on while the app runs. Every signal becomes a simulated trade at the live book (with fees)
in `data/signals.db`. Each trade is settled against Gamma's official outcome, and both the exit-rule
and hold-to-resolution PnL are recorded. Model predictions are also logged every 15s for every
window, so the calibration chart covers windows that didn't trade too.

### Validation status

Backtest, 2026-11-09: 1 day of polymarket.com data (288 × 5m + 96 × 15m windows), untrained
TWAP-aware prior, $50 stake, 1¢ half-spread, 2s latency, **after the volatility fix**
(volatility from 60-second returns; 1-second returns understated it about 6×).

| Sample | Model Brier | Market Brier | Following exit rules | Holding to resolution |
|---|---|---|---|---|
| All 385 windows | 0.1371 | 0.1406 | 365 trades, **+5.7%/$** (t = 1.8) | +12.6%/$ (t = 1.7) |
| Last 30% (out of sample) | 0.1241 | 0.1294 | 112 trades, +9.1%/$ (t = 1.1) | +2.7%/$ (t = 0.2) |

Before the fix the prior was overconfident: when it said 15%, UP happened 26% of the time, and
holding lost 7%/$. Now its predictions match reality across the range (15% → 9%, 85% → 87%).

Read this as **promising but unproven**:
- One day, a different exchange, and no result is statistically significant.
- Polymarket US has higher taker fees (0.095 vs 0.07).
- Judge on the paper trades (300+) on the exchange you'll actually use.

---|---|---|---|---|
| All 385 windows | 0.1372 | 0.1406 | 367 trades, 58.6% win, **+1.7%/$** (t = 1.1), max DD $228 | −7.0%/$ (t = −1.5) |
| Last 30% (out of sample) | 0.1227 | 0.1294 | 107 trades, **+2.0%/$** (t = 0.7) | −23%/$ (t = −3.2) |
| 15m only, all | | | 93 trades, 67.7% win, +4.0%/$ (t = 1.6) | |

Read this as **no demonstrated edge yet**:
- The model's probabilities score slightly better than the market mid, but the PnL is not
  statistically significant on one day.
- Holding to resolution loses. The exit rules (taking profit when the market catches up to fair
  value) are what keep it near break-even.
- The prior is overconfident in the tails, and it trades in almost every window. That's adverse
  selection: where it disagrees with the market, the market is usually right.
- `train` fitted logistic models for both durations. Neither beat the prior out of sample, so
  neither was saved; the app runs on the prior.
- Collect more days, rerun, and judge on the out-of-sample report and a few hundred paper-traded
  windows. Consider raising `signal.buffer` if trades stay this frequent.

---|---|---|---|
| Market mid | 0.1376 | – | – |
| Prior only | 0.1352 | +0.5%/$ (t = 0.13) | −31%/$ (t = −3.6) |
| Trained LR | 0.1378 | +1.3%/$ (t = 0.22) | +15%/$ (t = 1.2) |

Read this as **no demonstrated edge**:
- On the full day the prior is overconfident in the tails, and the trades it rated ~80% won ~60%.
  That's adverse selection: the market is usually right where they disagree.
- The trained model didn't beat the prior out of sample, so it isn't shipped.
- Collect more days, rerun, and judge on the out-of-sample report and a few hundred paper-traded
  windows.

---

## Files

```
app/feeds/       PriceFeed interface, sources, FeedManager (failover + synthetic fallback), quality monitor
app/ingest/      Gamma discovery + rules, CLOB books, Hyperliquid derivatives, RSS news, econ calendar
app/features.py  feature math        app/model.py   logistic model + explanations
app/signal.py    entry decision      app/exits.py   SELL/HOLD    app/fees.py  fee curve, book walking
app/engine.py    live loop           app/paper.py   paper trader + stats   app/db.py  SQLite
app/main.py      FastAPI + websocket web/           dashboard (plain HTML/JS)
app/notify.py    phone push alerts (ntfy / Telegram)
app/trading/     auto-trader: rules, risk, paper/live brokers, audit store, live gate
app/trader.py    kill switch / status CLI
app/preflight.py read-only live-readiness checks (python -m app.preflight)
app/test_order.py step-4 order test: rest + cancel, optional tiny real fill (python -m app.test_order)
app/env_guard.py refuse to start if .env is tracked by git
app/clock.py     clock check (NTP, falls back to the exchange's HTTPS clock; w32tm status on Windows)
app/keepawake.py stop idle sleep while running (Windows / macOS / Linux)
windows/         setup / start-bot / stop-bot / resume / status (.bat wrappers around .ps1)
app/modules/      strategy modules: base interface, crypto_updown, sports (markets, odds, research, module)
backtest/sports_report.py  sports CLV/ROI/calibration from recorded data
app/ingest/us_markets.py  Polymarket US discovery, rules check, order books
app/trading/us_broker.py  Polymarket US live orders (Ed25519-signed REST)
backtest/        collect / replay / train / report
tests/           tests: sports (de-vig, consensus, quotas, exits, CLV), features, TWAP math (Monte Carlo), fees vs docs table, rules, feed
                 parsers and staleness, degraded-data end-to-end, books, model, backtester
docs/ARCHITECTURE.md   research notes and design
docs/WINDOWS.md        Windows setup and running guide
```

## Known limitations

- Liquidation data isn't available from Hyperliquid's public API, so that feature is 0.
- The PolyBolt feed is implemented from Polymarket's docs but untested here (no credentials).
- The economic-calendar feed covers the current week and is filtered to USD high-impact releases. Add anything else to `events.yaml`.
- The news classifier is a transparent keyword lexicon. It is crude by design and shows its matches.
