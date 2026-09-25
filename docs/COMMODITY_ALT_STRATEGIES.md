# Alternative strategy families for crude, natgas and the base metals (run 2026-09-25)

`python3 -m markets.commodity.experiments.alt_strategy_study` -- because re-tuning the trend-breakout thresholds could not rescue these contracts
(docs/COMMODITY_CALIBRATION.md), two other families were tested with the same discipline: full session, real cost model, real Upstox margin, 10% risk on a
fixed Rs100k, 60% train / 40% test by date, pick on TRAIN only, judged on TEST (n>=15, net>0, PF>=1.2, and >=40% of train-profitable combos also profitable on TEST).
  A. VWAP mean-reversion (5-min): 72 combos.   B. Slower 15-min Donchian channel, traded with the break or faded: 32 combos.

## Result
| Contract | Mean-reversion | 15-min channel | Verdict |
|---|---|---|---|
| CRUDEOILM | 8/72 profitable on TRAIN, 0 on TEST; best-on-train TRAIN +37.2k PF1.21 -> TEST -62.8k PF0.68 | 0/32 on TRAIN | no edge in either family |
| NATGASMINI | 4/72, 0 on TEST; TRAIN +6.7k -> TEST -10.4k PF0.63 | 1/32, 0 on TEST; TRAIN +13.7k -> TEST -14.9k | no edge |
| ALUMINI, ZINCMINI, LEADMINI, NICKEL | (some combos passed the numbers) | (some passed) | EXCLUDED: illiquid -- see below |

Liquidity (5-min archive bars with zero traded volume / median volume per bar): crude 18% / 66, natgas 17% / 42, SILVERMIC 0.3% / 100, GOLDTEN 17% / 17,
ALUMINI 39% / 2, ZINCMINI 36% / 4, LEADMINI 80% / 0, NICKEL 90% / 0. LEADMINI and NICKEL "won" (LEAD channel-fade TRAIN +16.7k -> TEST +12.3k; NICKEL both
families, PF 2-3) only because their prices barely move between prints; a stale-price backtest fill is not a price anyone gets, and the half-tick slippage
assumption means nothing there. The study now refuses any contract with >30% zero-volume bars before running a single backtest.

Conclusion: crude and natgas have no edge in trend-breakout, VWAP mean-reversion or a slower channel; mean-reversion looks profitable in-sample and collapses out of
sample (crude TRAIN +37k -> TEST -63k). The base-metal minis are not tradable at this size. The system trades what has an edge and is liquid: SILVERMIC and GOLDTEN.
GOLDTEN caveat: median 17 lots per 5-min bar and 17% empty bars, against ~7 lots per trade -- watch the paper fills.

## What the literature says (searched 2026-09-25)
- Futures markets favour momentum; mean-reversion is a spot-market phenomenon (ScienceDirect, "Momentum and mean-reversion in commodity spot and futures markets").
- Intraday: first-half-hour returns predict the last half-hour in liquid US ETFs; in China's crude oil futures the intraday effect is REVERSAL of the overnight return
  (ScienceDirect S0264999319310417 -- abstract only, the full text was not readable); reversal strategies can be significant but returns may not cover fees.
- MCX daily backtest 2015-2026 (Zerodha, substack): Indian commodity futures mostly TREND; gold long-biased, silver/copper both ways, natural gas short-biased,
  crude mixed -- consistent with silver and gold working here and crude and natgas not.
- Falsification study on 14 OHLCV intraday-momentum families (arXiv 2605.04004, MNQ futures): none survived costs + out-of-sample + stability; gross edges are usually
  smaller than friction. A reminder that ~100 tried combinations plus a short window will produce lucky winners.
- Gap noted, not tested: an overnight-return reversal (China crude finding) and opening-range signals on MCX crude. Candidate for the next round, only if it can be
  validated on the same 60/40 split.

## Round 2: several strategies per symbol, chosen by phase (run 2026-09-25, `regime_switch_study.py`)
Hypothesis: a symbol has phases, so it needs a library of strategies plus a picker. Library of 7 fixed-parameter strategies (TREND = live rule, MR-A, MR-B, CH-BRK,
CH-FADE, ORB, ORB-FADE), two strictly walk-forward pickers: PERFORMANCE SWITCH (best trailing-L-day P&L, must be > 0, one strategy per day) and REGIME MAP (trend/range from the
prior 10 sessions' efficiency ratio; strategy-per-regime learned on the first half, applied to the second). 94 trading days, real margin, full session, Rs100k, 10% risk.

| Symbol | TREND alone | Best other alone | Switch L=10 | Switch L=20 | Regime map (test half) | TREND alone on those test days |
|---|---|---|---|---|---|---|
| SILVERMIC | +55.2k | ORB-FADE -2.8k | -42.6k | +22.5k | -10.1k | +26.3k |
| GOLDTEN | +43.1k | MR-B +9.2k | -37.5k | -30.9k | -34.1k | +28.5k |
| CRUDEOILM | -5.4k | ORB -11.6k | -44.9k | -56.5k | -37.4k | +11.2k |
| NATGASMINI | -16.1k | ORB-FADE -5.2k | -24.6k | -13.9k | -13.4k | -1.8k |

Result: on every symbol, both pickers did WORSE than simply running TREND (silver switch +22.5k vs TREND +49.7k on the same days; gold -31k to -37k vs +48k; crude -45k to -57k). Recent
success of a strategy did not predict its next-day success, and the efficiency-ratio regime learned in the first half did not carry to the second (crude "trend" -> MR-B, test -12.8k).
Every non-TREND strategy lost money on crude and natgas in this window, so there was nothing profitable for a picker to switch TO there.
Limits: only 94 days (one long phase may dominate), 7 strategies, one regime definition, one trailing-performance rule. Not shown: other regime signals (volatility level, session
phase, event days), which would need more history to validate. Re-run when the archive is longer (`python3 -m markets.commodity.experiments.regime_switch_study`).

## Round 3: 2.7 years on global prices (run 2026-09-25, `global_proxy_study.py`)
Data: Dukascopy free 1-minute history, 2024-01-01 .. 2026-09-24 (714 weekdays; 1-3 days per instrument failed after retries), turned into MCX-like 5-minute series (rupees at the day's USDINR,
MCX units, MCX session hours). PROXY: no MCX duty premium (real MCX silver ~10% above the proxy), no MCX spread/liquidity, CFD tick volume instead of exchange volume -- and the live TREND rule
uses a volume-surge filter, so TREND on the proxy is less faithful than the volume-free alternatives. Same library, costs, real-margin sizing and pickers as round 2; the live rule's backtest
compounds capital, so each calendar year restarts at Rs100,000 (otherwise a bad 2024 leaves no capital for later years).

Net by year, each strategy alone (Rs100k, 10% risk):
| | TREND | best alternative | all alternatives |
|---|---|---|---|
| GOLDTEN | 2024 -25.7k, 2025 +36.1k, 2026 +76.9k (total +87.4k) | ORB-FADE 2026 +31.6k, ORB 2025 +12.1k | lose in almost every year (MR -115k/-142k, channel -417k/-616k) |
| SILVERMIC | 2024 -90.1k, 2025 -83.1k, 2026 +66.0k (total -107.2k) | ORB-FADE -74k | all lose every year |
| CRUDEOILM | 2024 -38.8k, 2025 -58.5k, 2026 +67.0k (total -30.3k) | ORB -38k | all lose every year |
| NATGASMINI | 2024 -28.7k, 2025 -15.2k, 2026 +0.4k (total -43.4k) | ORB -55.8k | all lose every year |

Pickers: performance switch lost money on every symbol (gold -161k / -251k vs TREND +93k on the same days; silver -473k; crude -259k / -163k; natgas -243k / -238k); the regime map either stayed flat
(no strategy profitable in the learned half) or matched TREND at best (gold test -0.8k vs TREND +111k).

Read-across: (1) the 2026 profits of TREND on silver/crude/gold are concentrated in the last ~9 months, the strongly trending stretch (silver rose from ~Rs85k to Rs360k/kg); 2024 and 2025 lost on silver and
crude, and gold lost in 2024. The edge we measured on 4 months of real MCX data is regime-specific until proven otherwise. (2) None of the mean-reversion, channel or opening-range alternatives worked in any
year on any of the four, and choosing between strategies by recent performance or trend/range made things worse. (3) So more strategies per symbol did not help; what varies is whether the market is
trending strongly enough for TREND to pay.
Not tested: a volatility- or trend-strength ON/OFF gate for TREND itself (trade only when the underlying is in a strong multi-day trend). That is the next hypothesis this data can test, on the same year-by-year basis.

## Round 4: gating TREND on a strong multi-day trend (run 2026-09-25, `trend_gate_study.py`)
Six pre-set gates (efficiency ratio of the prior 10 or 20 sessions above 0.25 / 0.35 / 0.45), no selection among them, on the same 2.7-year proxy, year by year.
- GOLDTEN: every gate cuts profit (ungated +87.4k; gates +82.7k down to -2.8k) and none fixes 2024 (-4.5k to -36k).
- SILVERMIC: ER10>0.45 is best (2024 -4.5k, 2025 -21.8k, 2026 +69.1k, total +42.9k) but still loses in both early years; the other five gates total -127 to -127k.
- CRUDEOILM: gates shrink the losing years (ungated 2024 -38.8k, 2025 -58.5k; ER20>0.35: 2024 +0.9k, 2025 -1.3k, 2026 +43.1k) mostly by trading only 6-17% of days; ER10>0.25 beats ungated in every year (-10.8k, -22.4k, +71.8k).
- NATGASMINI: still negative overall under every gate (-5k to -33k).
Conclusion: no gate is robust across the four symbols. Gates mostly lower losses by trading less; they do not create profit in the bad years and they cost gold its edge. Not deployed. The remaining lever is forward evidence
from the live paper account, not another filter fitted to the same three years.
