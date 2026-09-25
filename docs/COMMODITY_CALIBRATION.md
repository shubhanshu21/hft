# Per-symbol commodity calibration (run 2026-09-25, FULL session as live)

`python3 -m markets.commodity.experiments.symbol_calibration_study [--refresh]` -- FULL session 09:00-23:30 like the live daemon (the backtest function's default is the evening window only; a first run on that default was discarded) -- every MCX contract whose ONE lot fits Rs100,000 of Upstox's real
MIS margin, 48 entry-threshold combinations each (min_adx x min_vol x min_ema_slope; exits fixed at 1.8/1.4), chosen on TRAIN 2026-05-18..07-31 and
judged on TEST 2026-08-01..09-24. Live 10% risk, leverage = min(10, Upstox's real leverage), real cost model. Adopt only if TEST n>=15, net>0, PF>=1.2
and better than today's thresholds.

Upstox margin per lot (measured 2026-09-25): CRUDEOILM 28.6k, NATGASMINI 34.5k, SILVERMIC 30.1k, GOLDTEN 14.0k, ALUMINI 32.4k, LEADMINI 14.4k,
ZINCMINI 41.5k, NICKEL 44.7k -- affordable. COPPER 326.6k, SILVERM 150.3k, GOLDM 139.4k -- not affordable. NICKELM / COPPERM are not in Upstox's MCX master.

| Contract | Today's thresholds TRAIN | Today's thresholds TEST | Best re-tune on TEST | Verdict |
|---|---|---|---|---|
| SILVERMIC | n114 +21.3k PF1.18 | n84 +23.4k PF1.26 DD20% | n87 +17.6k PF1.25 DD10% | keep today's (26 of 27 profitable combos also profit on TEST; the re-tune halves drawdown but earns less, so it fails the pre-set rule) |
| GOLDTEN | n72 +14.5k PF1.20 | n23 +23.1k PF2.17 | n25 +0.03k PF1.0 | today's thresholds profit in both windows (full period n95 +43.1k); re-tune rejected. Thresholds were set on gold data from this same period, so paper-forward evidence is still needed |
| CRUDEOILM | n77 -16.5k PF0.63 | n185 +16.3k PF1.20 | none | no edge on TRAIN; regime-dependent (full period n262 -5.6k) |
| NATGASMINI | n94 -17.3k PF0.49 | n23 +3.6k PF1.43 | none | no edge on TRAIN (full period n117 -16.1k) |
| ALUMINI | n47 -29.3k PF0.46 | n45 -30.2k PF0.40 | none | negative, only 3 months of data |
| LEADMINI | n0 | n27 -27.5k PF0.20 | none | negative, only 12 weeks of data |
| ZINCMINI | n39 -29.9k PF0.31 | n69 +14.8k PF1.22 | none | negative overall (full period n108 -22.9k), 3 months of data |
| NICKEL | n0 | n17 -11.7k PF0.34 | none | negative, only 2 months of data |

Long/short split (full period, live settings): SILVERMIC long +37.2k PF1.44 / short +19.0k PF1.13; GOLDTEN long +8.6k PF1.24 / short +34.4k PF1.58;
CRUDEOILM long -2.6k PF0.96 / short -3.0k PF0.94; NATGASMINI both sides lose (PF0.52 / 0.71); ZINCMINI both lose.

Result: no re-tuned threshold beat what a symbol already has. SILVERMIC and GOLDTEN are the only contracts profitable with today's thresholds in both windows; the rest
show no edge with these rules. The base metals only have 2-3 months of history (MCX serves ~1 month per contract): re-run once each has ~4 months
(ALUMINI/ZINCMINI ~late Oct 2026, LEADMINI ~Nov, NICKEL ~Dec).

Known gap: the LIVE entry rule (`markets/commodity/scalping/entry_signal.py`) only has keys crude/natgas/gold/silver, so ALUMINI/LEADMINI/ZINCMINI/NICKEL would
silently trade at crude's thresholds if put in DRYRUN_SYMBOLS, whereas the backtest gives them their own (uncalibrated, currently crude-equal) rows.
Do not add them to DRYRUN_SYMBOLS until they pass this study.

## Correction (2026-09-25, later the same day): GOLDTEN's cost assumption
The GOLDTEN figures above (full period +Rs43.1k, TEST +Rs23.1k) were run before the live daemon had sampled GOLDTEN's bid/ask. The cost model (core/slippage.adaptive_slippage_per_leg) uses half a tick per leg
until it has >= MIN_SAMPLES real spread observations, then the measured spread. After ~69 live samples (mean spread Rs30.8 on a ~Rs150,000 contract) the same backtest gives GOLDTEN n=95, net **+Rs18,766**,
fees Rs415/trade against gross Rs612/trade (silver: fees Rs363 vs gross Rs649; crude: fees Rs169 vs gross Rs148). Use the lower number; it is still positive but the cost margin is thin.
