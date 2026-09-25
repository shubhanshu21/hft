# What at a 5-minute bar's close predicts the next 15-60 minutes? (run 2026-09-25, `five_minute_signal_screen.py`)

We trade 5-minute bars, so a leading signal must be computable from bars up to the current close. Screen: every feature in markets/*/features.py plus returns over 1-24 bars, distance to the previous day's high/low,
position in the last 24-bar range and volume trend, each ranked (Spearman IC) against the forward return over 3 / 6 / 12 bars, computed PER MONTH; reported: mean IC, t across months, share of same-sign months, IC by calendar year,
top-vs-bottom decile forward return in bps. Pass = |t| >= 3, same sign every year, decile spread above the ~6 bps round-trip cost hurdle. Data: 2.7 years of global prices as MCX-like 5-minute bars (proxy, 33 months) and the real MCX archive (4 months).

## Result
No feature passes. The strongest and most CONSISTENT relationship on all four commodities, in every year and on real MCX, is short-term REVERSAL, not momentum: the last bar's return (ret_1), bar direction, close location in the bar,
RSI, position in the 24-bar range and slow momentum all have NEGATIVE IC against the next 15 minutes (silver proxy IC -0.028, t=-8.6, 88% of months, all three years; real MCX silver IC -0.091, t=-10.4;
gold, natgas, crude the same sign; also on the real archive). The size is far below cost: top-vs-bottom decile spreads are 0.1-1.5 bps on the proxy and 1-5 bps on real MCX (partly bid-ask bounce in thin 5-minute closes).
Nothing else (volume surge, ADX, VWAP distance, Bollinger width, opening-range distance, prior-day levels) is stable across years.

Implication: the live trend rule buys strength (RSI 70+, after a big bar), which is exactly where the most robust 5-minute pattern gives back a little over the following 15 minutes -- consistent with "losers reverse at once".

## Test derived from it: do not chase, rest a limit a fraction of a stop BETTER than the signal (opt-in `pullback_frac`, `pullback_bars` in backtest.py; default off)
Net Rs, real MCX first half / second half | proxy 2024 / 2025 / 2026 (each year restarts at Rs100k):
| Symbol / variant | Real | Proxy |
|---|---|---|
| CRUDEOILM baseline | -16,539 / +16,346 | -38,764 / -58,468 / +66,960 |
| CRUDEOILM pullback 0.5 stop, 3 bars | +9,865 / +8,204 | -2,546 / -5,557 / +10,814 |
| CRUDEOILM pullback 0.25, 3 bars | -968 / +2,592 | -30,568 / -41,994 / +37,722 |
| SILVERMIC baseline | +22,129 / +24,102 | -89,932 / -82,227 / +76,392 |
| SILVERMIC pullback 0.25, 3 bars | +9,545 / +27,186 | -68,186 / -47,344 / +137,708 |
| GOLDTEN baseline | -626 / +18,661 | -63,923 / -15,319 / +40,184 |
| GOLDTEN pullback 0.25, 3 bars | -11,932 / +14,074 | -5,436 / -26,643 / -19,141 |
| NATGASMINI baseline | -17,278 / +3,597 | -28,682 / -15,160 / +403 |
| NATGASMINI pullback 0.5, 3 bars | -3,869 / -3,989 | -341 / +858 / -2,410 |
By the pre-set rule (beat baseline in BOTH real halves and >= 2 of 3 proxy years) nothing passes. But the pattern is informative: the pullback entry mostly removes the LOSING periods (crude: every one of the 5 periods
is between -5.6k and +10.8k instead of -58k .. +67k; crude total on real +18.1k vs -0.2k, proxy +2.7k vs -30.3k; silver proxy total +22.2k vs -95.8k) and gives up part of the strong-trend gains (crude 2026 +10.8k vs +67.0k;
silver real first half 9.5k vs 22.1k). It cuts trade counts by 30-70% (crude 24 vs 77 in the real first half), so samples are small. The fill model assumes a resting limit is filled when price touches it (optimistic: no queue), and
skips signals whose bar also trades through the stop. Not deployed.

## Round 2: the pullback entry on EVERYTHING, with fill realism (run 2026-09-25, `pullback_everything_study.py`, shared logic in core/entry_pullback.py, all opt-in and off by default)
Fill assumptions: "touch" (filled when price reaches the limit; optimistic), and a haircut of h stop-distances the price must trade THROUGH the limit: 0.005 and 0.02 (a few ticks) and 0.1 (heavy: 10-80 ticks on most contracts).

### Equity (49 names, 2022-08..2026-09, live portfolio rules on fixed Rs100k; net Rs by calendar year 2022 / 23 / 24 / 25 / 26, PF) -- the strongest result
| Variant | 2022 | 2023 | 2024 | 2025 | 2026 | Total (trades) | PF | Verdict |
|---|---|---|---|---|---|---|---|---|
| baseline (live) | +54.5k | +299.2k | +40.3k | +48.4k | +95.6k | +537,960 (2,970) | 1.21 | - |
| 0.15, touch | +67.4k | +271.6k | +137.2k | +185.5k | +155.2k | +816,747 (2,433) | 1.40 | improves (4/5 years, both halves) |
| 0.15, haircut 0.005 | +66.7k | +262.8k | +126.1k | +183.5k | +149.5k | +788,594 (2,420) | 1.38 | improves |
| 0.15, haircut 0.02 | +63.7k | +239.5k | +78.0k | +178.7k | +99.8k | +659,645 (2,375) | 1.32 | improves |
| 0.25, touch / 0.005 / 0.02 | | | | | | +708,540 / +684,070 / +633,551 | 1.38 / 1.37 / 1.34 | 4/5 years; second half better, first half +/- |
| 0.50, touch / 0.02 | | | | | | +393,267 / +297,564 | 1.28 / 1.21 | worse than baseline |
| 0.15 / 0.25 / 0.35 / 0.50, haircut 0.1 | | | | | | +167k / +139k / +83k / -25k | 1.08 / 1.07 / 1.05 / 0.98 | worse |
A SHALLOW pullback (0.15-0.25 of a stop distance) raises PF from 1.21 to 1.32-1.40 and net by 18-52% with ~20% fewer trades, better in 4 of 5 years (2023 is worse) and in both halves, and survives a few ticks of fill
haircut; it collapses under the heavy 0.1 haircut. Caveat: the base rule's thresholds were tuned on this same history, and a live version needs pending limit orders (a runner change), so it is a candidate, not a deployment.

### Commodity (real MCX halves / proxy years) -- weaker
CRUDEOILM 0.15 (haircut <= 0.005): real -95 / +17,069 vs baseline -16,539 / +16,346; proxy -5.6k / -45.1k / +119.5k vs -38.8k / -58.5k / +67.0k: improves by the rule; at haircut 0.02 the real second half is 14.2k (< baseline)
and deeper pullbacks or the 0.1 haircut are worse. NATGASMINI 0.15 also passes the rule but stays negative (real -4.4k / +6.2k; proxy -21k / -12k / -2k): less loss, no edge. SILVERMIC 0.15 / 0.25: better in all three proxy
years (2026 +358k / +127k, compounding outlier) but the real first half is worse (+7.0k / +9.5k vs +22.1k). GOLDTEN: no variant beats baseline. Full grids: see the study output (`python3 -m markets.commodity.experiments.pullback_everything_study`).

### Currency (USDINR at 10x, real data): nothing improves
Trades fall from 46 to 4-6 in the first half (USDINR signals seldom pull back within 3 bars); net -1.5k..+0.3k vs +10.6k in the first half, mixed in the second (-3.3k baseline vs -1.4k..+0.8k).

### Deployed to paper trading (2026-09-25 night)
`core/entry_pullback.py` (PendingBook) is wired into `engine/live_dryrun.py::_try_enter`; the real-order runner (`engine/live_trading.py`) is untouched. Off unless `<SYMBOL|MARKET>_PULLBACK_FRAC` is set. Enabled in .env for
EQUITY (frac 0.15, haircut 0.02, 3 bars) and CRUDEOILM (0.15, 0.02, 3 bars). Silver, gold and USDINR stay at the signal price because none improved. A signal now logs "resting limit ... instead of chasing"; it fills when a later bar
reaches the limit (through it by the haircut) without running through the stop, and the position is recorded at the limit price with every level re-anchored; unfilled after 3 bars, or a new day, it is dropped (a restart also drops pending
limits). Compare paper results with the current behaviour over the next few weeks before trusting the backtest gain.
