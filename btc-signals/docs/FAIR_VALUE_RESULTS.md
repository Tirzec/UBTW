# Fair-value model: calibration and market test (2026-11-09)

Data: public BRTI series (5 days, 1 s; it IS the 60-second settlement average: X(T-1) equals the official
price to beat / settlement value in 120/120 markets checked) + Binance BTC/USDT 1 s as the raw-price proxy
(3 days). Settings chosen on the first 2 days, results below on the last day only (never used for tuning).

Chosen settings: {'sigma_step': 30, 'vol_mult': 1.15, 'dist': 't'}

## Calibration on the held-out day (every 10 s from 1/3 of each window to the end)

| | predictions | Brier (lower = better) | coin flip | calibration error |
|---|---|---|---|---|
| all | 11514 | 0.0870 | 0.2500 | 2.8% |
| 15-minute | 5754 | 0.0841 | 0.2500 | 3.3% |
| 5-minute | 5760 | 0.0900 | 0.2498 | 2.8% |
| last 60 s | 2304 | 0.0166 | 0.2499 | 1.7% |
| old approach (lagging index) | 11514 | 0.0984 | 0.2500 | 3.1% |

| predicted | n | actual |
|---|---|---|
| 0-10% | 2946 | 0.7% (predicted 2.5%) |
| 10-20% | 871 | 6.7% (predicted 14.8%) |
| 20-30% | 720 | 18.9% (predicted 24.9%) |
| 30-40% | 573 | 28.3% (predicted 35.0%) |
| 40-50% | 500 | 39.2% (predicted 44.8%) |
| 50-60% | 562 | 57.7% (predicted 55.0%) |
| 60-70% | 554 | 66.6% (predicted 65.2%) |
| 70-80% | 783 | 77.7% (predicted 75.2%) |
| 80-90% | 1127 | 87.3% (predicted 85.6%) |
| 90-100% | 2878 | 98.1% (predicted 97.2%) |

A recalibration fitted on the training days made the test day worse (error 2.8% -> 4.1%): the bias changes direction from day to day, so none is applied.

## Model vs. the market (same day, ~1-minute Polymarket price snapshots)

Brier: model 0.0980 vs market mid 0.0639 (15m: 0.0907 vs 0.0747; 5m: 0.1060 vs 0.0519). The market is more accurate.

PART 2 entry rule, buy at the ask, hold to resolution, taker fee included:

| | trades | win rate | break-even | avg win | avg loss | P&L per $1 |
|---|---|---|---|---|---|---|
| 15m, 5-pt edge | 46 | 26.1% | 31.0% | +0.534 | -0.240 | -13.4% |
| 5m, 10-pt edge | 57 | 15.8% | 24.0% | +0.650 | -0.206 | -33.0% |

Stricter thresholds lose more (15m: -23.5% per $ at 10 pts, -13.4% at 15 pts): the larger the
disagreement with the market, the more often the model is the one that is wrong.

Reproduce: `python -m backtest.calibrate_fair --data DIR` and `python -m backtest.market_edge --data DIR`.
