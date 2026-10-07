# Edge audit: does each live strategy beat random entries? (2026-10-06)

A backtest that makes money can still have no edge: the exits, the sizing or the market's drift may be doing the work. Test used here:
keep everything (instrument, days, exit rule, size, costs) and replace only the ENTRY by random ones, many times. A real signal beats
almost all random runs. Plus cost stress and per-period checks. Scripts: scratch studies summarised below; tools that stay in the repo:
`engine/exit_replay.py`, `engine/slippage_report.py`, `markets/crypto/experiments/active_strategy_lab.py`.

## Verdict

| Market | Real result | Beats random? | Higher costs | Decision |
|---|---|---|---|---|
| Crypto slow blend (1.5x) | +34%/yr 2021-26, +17% 2025-26 | timing beats 100% of 200 circularly-shifted copies (92% in 2025-26) | trades rarely | keep |
| Crypto breakout sleeve | +17.6%/yr 2021-26, +7.8% 2025-26 | beats 100% of random-time entries on random uptrend days (98% 2025-26); beats buying the open on those days (97%) | +2%/yr at 12 bp/side | keep |
| USDINR (chop gate, 20x) | +31.2k, 39 trades 2026-06..09 | direction beats 97% | +20.2k at 2x fees | keep |
| Equity, 49 names | +6.46 lakh, 2,394 trades 2022-26, every year positive | direction beats 100% of 34 random-direction runs; random direction AND time: 100% | +2.28 lakh at +2 bp/side (2024 negative); -3.99 lakh at +5 bp | keep -- execution cost is the risk |
| SILVERMIC | +37.6k, 112 trades | direction beats 97% | +3.4k at 2x fees; Jul+Aug made it all | keep, review 2026-10-19 |
| GOLDTEN | +5.5k | timing 46%, direction 75% | -35k at 2x fees (fees 41k of 46k gross) | **stopped** |
| CRUDEOILM | -12.4k | random timing does better (real beats 5%) | -55k | **stopped** |

"Random time, same direction" is NOT a fair test where the direction itself comes from a signal later in the day (a random earlier entry
then already knows the day's direction): it was run and is excluded for equity and the breakout for that reason.

## Commodities on other timeframes (global proxy 2024-01..2026-09 in MCX hours, fit to 2025-06, judged after; plus real MCX 2026-05..10)

US-open range breakout (18:30/19:30 IST): loses everywhere. 15-minute channel breakouts, same day: lose. Daily volatility breakout:
isolated positive cells whose neighbours lose (noise). Multi-day channels: strong 2025-26, deeply negative 2024 (a phase). Hourly EMA
trend, multi-day, after fixing a look-ahead in the first version (+218%/yr was the bug): only gold 20/100 positive on all three windows
(+3.8% / +20% / positive on real MCX) -- gold's bull market, consistent with the 23-year daily study (gold trend Sharpe ~0.4).
Why: MCX follows COMEX/NYMEX within the same minute (no information edge); a 5-minute gold move (3.4 bp) is under half a round trip
(8 bp); crude's 5-minute returns mean-revert (-0.058).

## Chronos (amazon-science/chronos-forecasting)

Not used. Tested zero-shot (chronos-bolt-small) on 2025-26: rank correlation of forecast vs realised return -0.04..+0.03 at every horizon
on BTC/ETH/SOL (1h), five NIFTY stocks (5m), USDINR and silver; sign right 47-52%; trading the sign loses 6-15 bp per trade after costs.
No predictive value for returns here.

## Execution cost (the thin part)

`python3 -m engine.slippage_report`: half-spreads are small (equity ~0.95 bp, commodity ~1.15, USDINR ~3.6); stop overshoot typical
~1 bp. Equity's live entry price, however, sat a median ~9 bp worse than the signal bar's close on the first 9 trades -- more than the
edge's whole cushion. Tested systematically on 1-minute data (49 stocks, 2026-05..10, 160 signals, live entry function on the forming bar vs
the completed bar's close, same exit): drift median 0.0 bp; live mid-bar entry +6.0 bp/trade gross vs +3.4 waiting for the close (on the
131 that still signal at the close: +8.9 vs +3.4; the 29 that vanish by the close: -6.7). The 9-trade figure was small-sample noise; live
entries stay as they are, and the report keeps measuring.
