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

Requires Python 3.11+.

```bash
cd btc-signals
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # optional: Polymarket API keys for the PolyBolt feed
python -m pytest                # 72 tests, about 1 s
python -m app.main              # open http://127.0.0.1:8000
```

Click **Enable alerts** once in the browser to allow sound and desktop notifications.

Start the app **before** a window begins. The price to beat comes from Chainlink at the boundary,
and Gamma only publishes it after the window closes. Windows already running at startup show
"price to beat unknown" (NO TRADE) unless the previous window's official final price is out.

### Network hosts used
Polymarket: `gamma-api.polymarket.com`, `clob.polymarket.com`, `ws-subscriptions-clob.polymarket.com`,
`ws-live-data.polymarket.com` (RTDS), `ws-live-v2.polymarket.com` (PolyBolt, optional),
`data-api.polymarket.com` (backtest), `polymarket.com` (display-only cross-check).
Spot: `ws-feed.exchange.coinbase.com`, `ws.kraken.com`. Derivatives: `api.hyperliquid.xyz`.
News: CoinDesk, Cointelegraph, Decrypt, The Block RSS. Calendar: `nfs.faireconomy.media`.
Backtest bars: `data-api.binance.vision`. Binance's trading API is geo-blocked in the US and is off by default.

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

First run, 2026-11-09: 1 day of data (288 × 5m + 96 × 15m windows), untrained TWAP-aware prior,
$50 stake, 1¢ half-spread, 2s latency.

| Sample | Model Brier | Market Brier | Following exit rules | Holding to resolution |
|---|---|---|---|---|
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
backtest/        collect / replay / train / report
tests/           72 tests: features, TWAP math (Monte Carlo), fees vs docs table, rules, feed
                 parsers and staleness, degraded-data end-to-end, books, model, backtester
docs/ARCHITECTURE.md   research notes and design
```

## Known limitations

- Liquidation data isn't available from Hyperliquid's public API, so that feature is 0.
- The PolyBolt feed is implemented from Polymarket's docs but untested here (no credentials).
- The economic-calendar feed covers the current week and is filtered to USD high-impact releases. Add anything else to `events.yaml`.
- The news classifier is a transparent keyword lexicon. It is crude by design and shows its matches.
