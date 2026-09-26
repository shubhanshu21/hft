# Slower commodity ideas for a small account (run 2026-09-26, `daily_portfolio_and_spread_study.py`)

Data: Yahoo daily global futures 2003-2026 (proxy for MCX; no MCX premium), train 2003-2018 / test 2019-2026. Costs: 5 bps per unit of position change (trend), 15 bps round trip on the ratio spread (both legs).

## 1. Diversified daily trend following (time-series momentum 63/126/252-day, vol-targeted 10% per instrument, equal weight over gold, silver, crude, natgas; the four whose one lot fits Rs100k)
| | Annual return | Vol | Sharpe | Max DD |
|---|---|---|---|---|
| Gold | +3.4% (test +6.3%, last 3y +14.6%) | 8.9% | +0.38 (test +0.68) | 23% |
| Silver | +0.2% | 8.9% | +0.02 | 46% |
| Crude | +1.4% (test -1.0%) | 8.8% | +0.16 (test -0.11) | 29% |
| Natgas | -1.6% | 8.5% | -0.18 | 38% |
| **Portfolio of 4** | **+0.8%** (test +1.1%) | 5.4% | **+0.15** (test +0.22) | 16% |
The portfolio is positive in 9 of the 17 years since 2010 (2017 -8%, 2023 -7%, 2025 +10%). Diversifying across the four did not produce a usable edge: only gold trends reliably, and gold's recent gains track its multi-year bull run.

## 2. Gold-silver ratio mean reversion (log ratio z-score, 60/120/250-day lookback, entry z 1.5/2.0/2.5, exit |z| < 0.5 or 60 days)
Nine parameter cells, all shown: per-trade net returns range from -5.0% to +2.9%, t-statistics between -1.4 and +0.7 (train) and -0.6 and +0.7 (test), 8-46 trades per test cell; the sign flips between train and test in most cells.
Nothing is distinguishable from luck.

## Conclusion
Neither slower idea works for a Rs100k commodity account. The one persistent pattern is gold's own trend (Sharpe +0.4 to +0.7), which is small and mostly the gold bull market. Consistent with docs/SWING_RESEARCH.md (12-month momentum test PF 1.85 but t = 1.5).
