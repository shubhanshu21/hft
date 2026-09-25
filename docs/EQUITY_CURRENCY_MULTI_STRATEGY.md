# Equity and currency: several strategies picked by phase (run 2026-09-25)

Question (from the user): every symbol has phases, so should a market run several strategies and switch? Same library and pickers as docs/COMMODITY_ALT_STRATEGIES.md round 2:
TREND (the live rule), MR-A / MR-B (VWAP + RSI mean reversion), ORB / ORB-FADE (first-30-minute range, broken / faded), plus channel break/fade for currency. Pickers, strictly walk-forward:
performance switch (best trailing-L-day P&L, must be > 0) and regime map (trend/range from the prior 10 sessions' efficiency ratio, strategy per regime learned on the first half).

## NSE equity -- 49 NIFTY names, 4 years of real 5-min data (2022-08-01 .. 2026-09-24, 1,029 days)
`python3 -m markets.equity.experiments.multi_strategy_study`. Live portfolio rules (one Rs100k pool, max 3 open, real 5x MIS, real costs, 4% risk), on a FIXED Rs100k (no compounding) so
daily P&L is independent of earlier results.

| Strategy | Trades | Net (4 yrs) | PF | Win |
|---|---|---|---|---|
| TREND (live rule) | 2,970 | **+Rs537,960** | 1.21 | 65.9% |
| MR-A | 12,942 | -Rs1,987,063 | 0.73 | 43.6% |
| MR-B | 7,840 | -Rs1,534,626 | 0.77 | 46.0% |
| ORB | 4,535 | -Rs389,363 | 0.86 | 37.6% |
| ORB-FADE | 5,907 | -Rs1,001,188 | 0.71 | 45.5% |

By year, TREND: 2022 +54k, 2023 +299k, 2024 +40k, 2025 +48k, 2026 +96k -- positive in all five years. Every other strategy lost in every year.
Pickers: performance switch L=10 -Rs188k, L=20 +Rs149k, L=40 +Rs142k, versus TREND alone on the same days +Rs548k / +Rs530k / +Rs492k. Regime map: learned TREND for BOTH regimes
(TREND +152k in trend / +158k in range on the train half; every alternative lost in both), TEST net +Rs228k = TREND alone.
Conclusion: on 4 years, the market phase does not change which strategy is best; TREND is the only one with an edge, and switching only gives some of it away.
Caveat: the TREND thresholds were tuned on this same history (3 train/test folds, 2026-09-19), so its +538k is in-sample; the finding that the alternatives lose is not affected. The composite
phase signal is one definition (efficiency ratio of the equal-weighted universe); volatility or event regimes are untested. Fixed capital means losing strategies keep trading full size,
so their drawdown percentages are meaningless and not shown.

## NSE currency (USDINR)
`python3 -m markets.currency.experiments.multi_strategy_study`.
A. Real Upstox 5-min data, 2026-06-02 .. 09-24 (79 days): TREND +Rs507 (about break-even), MR-A -1.6k, MR-B -3.9k, CH-BRK -17.2k, CH-FADE -3.1k, ORB -4.3k, ORB-FADE -6.2k.
Performance switch -5.7k (L=10) / -0.6k (L=20); regime map (test half) -4.8k. Low power (79 days).
B. Two-year hourly proxy (Yahoo USDINR=X spot, NSE hours, 2023-12-11 .. 2026-09-25, 725 days, split 2025-08-11): a 12/20/32-bar channel traded WITH or AGAINST the break, 12 variants: all 12 lose
on TRAIN (net -10k to -29k, PF 0.46-0.65) and all 12 lose on TEST (PF 0.43-0.94; the breakout variants lose least, -2.6k to -8.3k). No edge in either direction.
USDINR's 5-minute range is ~0.03%, so costs (Rs120 round trip at 5 lots, measured with the cost model: brokerage 71, slippage 38, exchange etc. 11) are large against a ~0.03% bar. Conclusion: no strategy we can build from price alone beats costs on USDINR; keep it on paper only as a low-risk
control, or drop it to free margin.

## Where more data comes from
- Equity: already 4 years (Upstox, 5-min). Nothing needed.
- Currency: Upstox ~4 months; Yahoo USDINR=X gives 2 years of hourly and 60 days of 5-min (spot, not the NSE future). Dukascopy has no USD/INR. No free multi-year 5-min NSE currency source found.
- Commodity: Upstox ~4 months. Dukascopy's free 1-minute history (gold, silver, WTI, natgas, Brent, copper) for the underlying prices MCX contracts follow;
  services/data/dukascopy.py downloads it, markets/commodity/experiments/global_proxy_study.py re-runs the commodity study on it (results in docs/COMMODITY_ALT_STRATEGIES.md round 3).
