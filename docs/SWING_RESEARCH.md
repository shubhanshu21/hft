# Swing-strategy research (equity, commodity, currency)

Reproduce: `cd backend && python3 -m markets.swing_research`. Engine: `core/swing_engine.py`. Data: `services/data/yahoo.py`.
Run on 2026-09-23.

## Bottom line

| Market | Verdict | What was added |
|---|---|---|
| **Equity** | **No swing rule survives out-of-sample.** Great-looking numbers on the long Yahoo history are survivorship-biased and regime-dependent; on real Upstox data every rule turned negative in the test window. | nothing |
| **Currency** | **Nothing works.** Trend rules lose money; mean-reversion is flat. | nothing |
| **Commodity** | **Weak, unproven edge.** 12-month time-series momentum: test PF 1.85 (229 trades), survives 4x costs, but t = 1.5, carried by gold and copper. | `markets/commodity/swing` — **paper-only candidate, off by default** |

Nothing here justifies real money. The one addition is a hypothesis worth collecting paper-trading evidence on.

## Candidate rules and why they were chosen

Chosen from published, widely replicated ideas rather than searched for; each is tested with two parameter sets from the literature, not tuned.

* **Donchian channel breakout** (20/10 and 55/20 days) — the classic trend-following system; reported to work on commodities and currencies but not stocks ([QuantifiedStrategies](https://www.quantifiedstrategies.com/donchian-channel/)).
* **EMA trend** (20/50 and 50/200) with an ATR stop — standard moving-average trend following.
* **Time-series momentum** (126 / 252 days) — an asset's own past return predicts its next, across equity, currency and commodity futures ([Moskowitz, Ooi & Pedersen, JFE 2012](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463)).
* **RSI(2) pullback in an uptrend** (thresholds 5 / 10, above the 200-day average) — Connors' short-term mean reversion for equities ([QuantifiedStrategies](https://www.quantifiedstrategies.com/rsi-2-strategy/)).
* **Trend pullback** (RSI14 dip inside a 50/200 uptrend).

## Method

* **No lookahead:** a signal uses data up to a day's close and is executed at the *next* day's open. Stops are checked against each later day's range, and a gap through the stop fills at the open, not the stop (tests in `tests/test_swing.py` prove both, and that appending future data never changes past trades).
* **Costs:** each trade pays a round trip from this project's own cost models plus slippage — equity delivery **0.402%** (STT 0.1% each side, stamp duty, DP charge, brokerage, 5 bps/side slippage; rates checked against [Upstox](https://upstox.com/calculator/brokerage-calculator/) on 2026-09-23), commodity futures 0.082%, currency futures 0.045%.
* **Train / test:** the best parameter set per rule is picked on the **TRAIN** period only (highest t-statistic with >=30 trades). TEST is untouched. Split at 2018-12-31 for the long histories; 2024-06-30 for the real Upstox equity check.
* **Universe:** equity = the 49 NIFTY50 names, long-only (delivery cannot short); commodity = crude, gold, silver, natural gas, copper, long and short; currency = USDINR, EURINR, GBPINR, JPYINR, long and short.
* **Data sources:** equity two ways — Yahoo `.NS` adjusted prices 2005–2026 (long) and the **real Upstox 5-minute archive resampled to daily** 2022–2026 (independent). Commodity and currency use Yahoo 2003–2026, because Upstox has only 1–4 months of history for those.

## Results (all trades net of costs, unlevered)

Each cell: trades / average net return per trade / profit factor / t-statistic of the per-trade return (|t| below 2 is indistinguishable from luck).

### equity (Yahoo NSE, 2005-26)

49 symbols, round-trip cost 0.402%. Average buy-and-hold over the same windows: TRAIN +2140%, TEST +246%.

| Rule | Parameters | TRAIN: trades / avg per trade / PF / t | TEST: trades / avg per trade / PF / t | |
|---|---|---|---|---|
| Donchian breakout | n=20, exit_n=10 | 2248 / +2.84% / 1.81 / +7.5 | 1550 / +1.42% / 1.51 / +4.6 | chosen on TRAIN |
| Donchian breakout | n=55, exit_n=20 | 1149 / +5.89% / 2.33 / +6.2 | 822 / +2.89% / 1.80 / +2.8 |  |
| EMA trend | fast=20, slow=50 | 1484 / +7.87% / 2.83 / +7.7 | 940 / +5.78% / 2.73 / +4.6 | chosen on TRAIN |
| EMA trend | fast=50, slow=200 | 554 / +34.10% / 6.84 / +3.2 | 391 / +14.03% / 4.04 / +4.6 |  |
| Time-series momentum | lookback=126 | 2866 / +4.26% / 2.74 / +7.0 | 1901 / +2.41% / 2.09 / +4.4 | chosen on TRAIN |
| Time-series momentum | lookback=252 | 1929 / +9.14% / 4.34 / +3.0 | 1288 / +4.07% / 2.84 / +4.1 |  |
| RSI(2) pullback | thr=5 | 2378 / +0.21% / 1.15 / +2.2 | 1511 / +0.01% / 1.01 / +0.2 | chosen on TRAIN |
| RSI(2) pullback | thr=10 | 4341 / -0.04% / 0.97 / -0.6 | 2824 / -0.06% / 0.94 / -1.0 |  |
| Trend pullback (RSI14) | thr=35 | 1295 / -0.39% / 0.71 / -4.1 | 913 / -0.09% / 0.89 / -1.3 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=45 | 8220 / -0.36% / 0.71 / -8.2 | 5578 / -0.30% / 0.67 / -10.1 |  |

### equity (real Upstox, 2022-26)

49 symbols, round-trip cost 0.402%. Average buy-and-hold over the same windows: TRAIN +54%, TEST +3%.

| Rule | Parameters | TRAIN: trades / avg per trade / PF / t | TEST: trades / avg per trade / PF / t | |
|---|---|---|---|---|
| Donchian breakout | n=20, exit_n=10 | 243 / +2.51% / 1.99 / +2.9 | 438 / -1.07% / 0.66 / -3.4 | chosen on TRAIN |
| Donchian breakout | n=55, exit_n=20 | 149 / +4.23% / 2.44 / +2.8 | 234 / -1.83% / 0.54 / -3.7 |  |
| EMA trend | fast=20, slow=50 | 157 / +8.82% / 4.80 / +4.5 | 315 / -0.78% / 0.77 / -1.7 |  |
| EMA trend | fast=50, slow=200 | 76 / +17.87% / 7.82 / +4.7 | 162 / -1.81% / 0.65 / -2.0 | chosen on TRAIN |
| Time-series momentum | lookback=126 | 237 / +6.33% / 5.01 / +4.4 | 616 / -0.61% / 0.70 / -2.6 | chosen on TRAIN |
| Time-series momentum | lookback=252 | 149 / +6.65% / 3.94 / +3.8 | 524 / -0.83% / 0.68 / -2.6 |  |
| RSI(2) pullback | thr=5 | 235 / +0.26% / 1.32 / +1.5 | 422 / -0.39% / 0.69 / -2.6 |  |
| RSI(2) pullback | thr=10 | 458 / +0.25% / 1.32 / +2.2 | 742 / -0.31% / 0.74 / -2.8 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=35 | 108 / -0.13% / 0.81 / -0.8 | 285 / -0.25% / 0.67 / -2.6 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=45 | 872 / -0.06% / 0.91 / -0.9 | 1575 / -0.39% / 0.56 / -8.0 |  |

### commodity (USD futures x INR)

5 symbols, round-trip cost 0.082%. Average buy-and-hold over the same windows: TRAIN +284%, TEST +262%.

| Rule | Parameters | TRAIN: trades / avg per trade / PF / t | TEST: trades / avg per trade / PF / t | |
|---|---|---|---|---|
| Donchian breakout | n=20, exit_n=10 | 646 / +0.11% / 1.04 / +0.3 | 306 / -0.62% / 0.85 / -0.9 | chosen on TRAIN |
| Donchian breakout | n=55, exit_n=20 | 312 / +0.15% / 1.04 / +0.2 | 146 / +0.47% / 1.09 / +0.3 |  |
| EMA trend | fast=20, slow=50 | 440 / +0.15% / 1.04 / +0.2 | 209 / +0.29% / 1.06 / +0.2 |  |
| EMA trend | fast=50, slow=200 | 174 / +0.99% / 1.17 / +0.5 | 98 / +3.58% / 1.56 / +1.0 | chosen on TRAIN |
| Time-series momentum | lookback=126 | 750 / +0.07% / 1.03 / +0.2 | 362 / +0.54% / 1.20 / +0.7 |  |
| Time-series momentum | lookback=252 | 546 / +0.41% / 1.17 / +0.6 | 229 / +2.37% / 1.85 / +1.5 | chosen on TRAIN |
| RSI(2) pullback | thr=5 | 365 / +0.19% / 1.14 / +0.8 | 192 / +0.52% / 1.47 / +1.9 |  |
| RSI(2) pullback | thr=10 | 784 / +0.25% / 1.20 / +1.7 | 387 / +0.06% / 1.04 / +0.3 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=35 | 222 / +0.08% / 1.11 / +0.5 | 88 / +0.03% / 1.04 / +0.1 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=45 | 1630 / -0.17% / 0.82 / -2.7 | 778 / -0.04% / 0.96 / -0.4 |  |

### currency (INR pairs)

4 symbols, round-trip cost 0.045%. Average buy-and-hold over the same windows: TRAIN +38%, TEST +28%.

| Rule | Parameters | TRAIN: trades / avg per trade / PF / t | TEST: trades / avg per trade / PF / t | |
|---|---|---|---|---|
| Donchian breakout | n=20, exit_n=10 | 263 / -0.20% / 0.86 / -0.8 | 128 / -0.58% / 0.58 / -2.4 |  |
| Donchian breakout | n=55, exit_n=20 | 138 / -0.02% / 0.99 / -0.0 | 64 / -0.85% / 0.52 / -2.0 | chosen on TRAIN |
| EMA trend | fast=20, slow=50 | 269 / -0.23% / 0.87 / -0.5 | 179 / -0.45% / 0.64 / -2.2 | chosen on TRAIN |
| EMA trend | fast=50, slow=200 | 93 / -1.78% / 0.61 / -1.0 | 54 / +0.38% / 1.22 / +0.5 |  |
| Time-series momentum | lookback=126 | 484 / -0.32% / 0.74 / -1.0 | 314 / -0.21% / 0.71 / -1.7 |  |
| Time-series momentum | lookback=252 | 293 / -0.49% / 0.68 / -1.0 | 204 / +0.04% / 1.05 / +0.2 | chosen on TRAIN |
| RSI(2) pullback | thr=5 | 268 / -0.02% / 0.95 / -0.3 | 153 / +0.03% / 1.09 / +0.4 | chosen on TRAIN |
| RSI(2) pullback | thr=10 | 546 / -0.08% / 0.84 / -1.3 | 332 / -0.02% / 0.96 / -0.3 |  |
| Trend pullback (RSI14) | thr=35 | 190 / +0.03% / 1.11 / +0.5 | 58 / -0.01% / 0.97 / -0.1 | chosen on TRAIN |
| Trend pullback (RSI14) | thr=45 | 1211 / -0.12% / 0.69 / -1.5 | 630 / -0.03% / 0.88 / -1.1 |  |

## What the robustness checks showed

**Equity — regime-dependent and survivor-biased.** The 49 stocks are today's index constituents, i.e. the survivors; their average buy-and-hold return over the training window was +2,140%, so any long-only trend rule looks superb. By year, both EMA-trend and Donchian **lose in 2008, 2011, 2015 and 2018–19**, and are flat-to-negative in **2024–2026** (EMA-trend average per trade: −0.3%, +0.7%, −0.9%; Donchian: −0.6%, −0.7%, −1.4%), while earning almost everything in a few bull years (an average of +61% per trade in 2009, +31% in 2020). On the real Upstox data, every rule that scored PF 2–8 in training scored **PF 0.54–0.77 in the 2024-07 to 2026-09 test**. A market filter (only enter while the NIFTY50 is above its 200-day average) did **not** help: in that window the index stayed above its average (a choppy range, not a downtrend), so the filter changed almost nothing (test PF 0.57–0.62). The short-term RSI(2) rules net at most +0.2% per trade *after* the 0.40% cost (about +0.6% gross), which is +0.0% in the long-history test and negative on the real Upstox test; delivery costs consume most of what a 3-day trade can earn.

**Currency.** Trend rules are negative in both windows (e.g. Donchian 20/10 test PF 0.58, EMA 20/50 test PF 0.64). The rupee is a managed currency and does not trend the way the literature's freely floating pairs do. Nothing to add.

**Commodity.** Three rules are positive in aggregate, none statistically significant (t = 1.0–1.7):

| Rule (2003–2026, all symbols) | Trades | Avg / trade | PF | t | TEST PF (2019+) | TEST PF at 2x / 4x costs | Positive years | By symbol |
|---|---|---|---|---|---|---|---|---|
| Time-series momentum 252 | 769 | +1.03% | 1.42 | +1.5 | 1.85 (n=229) | 1.81 / 1.72 | 12 of 23 | gold 3.72, copper 2.24, silver 1.74, crude 1.13, **nat-gas 0.71** |
| EMA trend 50/200 | 267 | +2.00% | 1.33 | +1.0 | 1.56 (n=98) | 1.54 / 1.50 | 9 of 24 | gold 4.15, copper 2.13, silver 1.57, crude 1.10, **nat-gas 0.60** |
| RSI(2) pullback, threshold 5 | 557 | +0.30% | 1.23 | +1.7 | 1.47 (n=192) | 1.39 / 1.23 | 13 of 24 | nat-gas 1.51, copper 1.64, crude 1.05, silver 0.99, **gold 0.88** |

Trend rules win only 20–30% of the time and earn through a few large winners, so they need many years — or many more markets — to show significance; this is expected of the style, and also the reason not to trust it yet. Gold and copper carry the trend results; natural gas loses on every rule (this project already dropped it from live trading). **Time-series momentum 252 was added** because it has the most trades, works on 4 of 5 symbols, was chosen on TRAIN before TEST was seen, and holds up under 4x costs.

## Limitations — read before trusting any number

* **Commodity and currency use a proxy**: global futures in USD converted with USDINR. MCX contracts can differ by the import-parity premium, and Yahoo's continuous futures are not roll-adjusted (crude's roll gaps add noise).
* **Survivorship bias** in every equity number (today's constituents only).
* **One train/test split**, and many rule/market combinations were examined; with that many looks a t-statistic near 2 appears by chance. Nothing was tuned on TEST, but "best of several" is still selection.
* **Per-trade statistics, not a portfolio simulation**: no concurrency limits, position sizing, or portfolio drawdown are modelled.
* The real-data equity test window (2024-07 to 2026-09) is short and was a flat market, which is unfavourable to trend rules by construction — and that is exactly what the live system will face if it continues.

## The commodity swing strategy

`markets/commodity/swing/strategy.py` — hold long while the 252-day return is positive, short while negative, exit on a sign flip or a 3 x ATR stop; signals use completed daily bars and act in the first hour of the next MCX session. Because Upstox has no 252-day history for the current contract, the *direction and stop distance* come from the proxy series while sizing and stops use the real MCX price. It is **off** until named in `COMMODITY_STRATEGIES`; enable it in paper trading (e.g. `COMMODITY_STRATEGIES=scalping,swing`) to collect real evidence. If the proxy download fails it opens nothing. Shares the margin pool with the scalpers, so one margin-bound scalp can leave no room for a swing entry.

---

# Wider search: 34 rule variants, with luck controlled (2026-09-24)

The first study tested 5 rule families. It was fair to ask whether more indicators and chart formations would change the
answer, so this widened it to **21 rule families / 34 variants** (31 where there is no volume or cross-section):

| Group | Rules |
|---|---|
| Trend | Supertrend (2 settings), Ichimoku cloud, MACD cross, Keltner breakout, cross-sectional relative strength (top 20% by 126 / 252-day return) |
| Breakout | Bollinger squeeze breakout, volume-confirmed breakout, pivot-high (resistance) breakout (2), flag (pole + tight consolidation) |
| Reversal / retracement | Double bottom / double top, Fibonacci 38-62% pullback |
| Pullback | IBS (2), Bollinger mean reversion (2) |
| Candlesticks | Bullish/bearish engulfing, hammer / shooting star, morning / evening star, each with and without a pullback context |
| From study 1 (re-run identically) | Donchian, EMA trend, time-series momentum, RSI(2), trend pullback |

Not covered: head-and-shoulders, triangles, wedges, harmonic patterns (no objective, non-repainting definition that I could
defend). Code: `core/swing_rules_ext.py`, `markets/swing_research_ext.py`; run `python3 -m markets.swing_research_ext`.

## What makes a wide search trustworthy (and what was done)

Testing 34 variants means a few will look good by luck. So: the rule list was fixed in code **before any result was seen**;
significance is measured on **months** (49 stocks trading the same days are not independent), not raw trades; a single result
must clear a **Bonferroni bar (t >= 3.18 for 34 variants)** to be a PASS, and t >= 2 is only "watch"; **24 random-entry rules**
run through identical stops/exits/costs show what luck looks like in each market; and a rule has to be positive in most test years.
Pivots are confirmed 3 bars late, the cloud is not shifted forward, and `tests/test_swing_ext.py` proves no indicator at bar i
changes when later bars change.

## Results

| Market | Best variants (test) | Random-entry controls (test) | Verdict |
|---|---|---|---|
| Equity, real Upstox 2022-26 (49 stocks) | **All 34 negative** (avg -0.25% to -1.9% per trade, PF 0.45-0.79). 30 of the 34 were positive on train | best t +0.6, mean -0.32%/trade | Nothing works in the last 27 months, for any rule type |
| Equity, Yahoo 2005-26 | Trend family only: time-series momentum 126 (t +3.1, +2.5%/trade), EMA 20/50 (t +3.1), relative strength 126 (t +3.0), EMA 50/200 (t +3.0), Supertrend (t +2.6-2.7) | best t +2.3, 21% reach t>=2, mean +0.31% | **watch**, none PASS (bar 3.18) |
| Commodity (proxy) | bb_meanrev 2.0 (t +2.3, train t -0.3), tsmom 252 (t +2.0, train t +0.6) | best t **+3.3**, 17% reach t>=2 | Indistinguishable from luck |
| Currency | Nothing above t +1.4; 7 variants at t <= -2.3 | best t +1.8 | Nothing works |

**Candlestick patterns, flags, double bottoms, Fibonacci pullbacks, IBS, Bollinger squeeze / mean reversion, Ichimoku, MACD,
Keltner, volume breakouts and pivot breakouts: none earn a credible edge in any market after costs.** On the long equity history
they sit at t of about -3 to +1; on real 2022-26 data they are all clearly negative.

## Why the trend family "watches" but is not tradeable

By-year average net return per trade (Yahoo, 49 stocks; buy & hold in the last column):

| Year | tsmom 126 | EMA 20/50 | Rel. strength | Supertrend | Buy & hold |
|---|---|---|---|---|---|
| 2016 | +4.1 | +6.6 | +7.5 | +3.8 | +11.7 |
| 2017 | +3.9 | +9.1 | +7.5 | +3.5 | +38.3 |
| 2018 | -1.2 | -0.4 | -1.3 | 0.0 | +6.2 |
| 2019 | -0.7 | -1.0 | -1.6 | +0.4 | +13.3 |
| 2020 | +13.4 | +31.5 | +25.7 | +18.8 | +23.5 |
| 2021 | +2.7 | +3.3 | +2.0 | +4.0 | +39.0 |
| 2022 | +0.9 | +0.2 | +1.4 | 0.0 | +8.9 |
| 2023 | +4.5 | +13.6 | +10.0 | +5.5 | +29.5 |
| 2024 | -0.7 | -0.3 | -3.5 | -0.3 | +11.2 |
| 2025 | +0.2 | +0.7 | +0.8 | +0.5 | +15.6 |
| 2026 | -0.7 | -0.9 | -1.3 | -1.5 | -3.5 |

1. **The edge lives in a few years** (2016-17, 2020-21, 2023). Outside them it is about zero or negative, including 2024 and 2026,
   which is exactly the real-data window.
2. **Survivorship bias.** The universe is today's NIFTY 50, i.e. companies that survived and grew. Trend-following on hindsight
   winners flatters every number above; there is no delisted-stock data to correct it.
3. **The real 2022-26 Upstox test is negative for all of them,** and even the random controls lose there.
4. **The Bonferroni bar was not cleared** (best t +3.1 vs 3.18).

## Conclusion

More indicators and formations did not produce a profitable swing strategy; the extra search only produced more variants that
failed out of sample. The one persistent pattern is that long-only trend-following makes money in strong bull years and
nothing otherwise, which is a market-beta effect, not an edge that can be traded on demand. **Nothing is enabled.** The next
evidence that would change this: a longer *real* (not proxy) history including delisted stocks, or paper results over a full
market cycle for the commodity time-series-momentum candidate.
