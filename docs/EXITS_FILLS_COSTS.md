# Exits, fill realism, costs and instruments (2026-09-24)

Backtests: real Upstox 5-minute data, 2026-05-18 .. 2026-09-23 (about 4 months), split into two halves (first: to 2026-07-19, second:
from 2026-07-20). Scripts: `markets/commodity/experiments/{exit_structure,direction_regime,cost_share}_study.py`. Small samples: read
these as "no evidence of a gain", not as proof.

## 1. A real bug: breakeven-lock exits could fill at prices that never traded

The breakeven / trail arm moved the stop to `entry + 0.20%`. The arm fires when price reaches a trigger level, and for some markets that
level is closer than 0.20% (currency: ~0.04%). The stop then sits **above the market**, and the paper fill happens at a price that never
traded. Real example, USDINR 2026-09-23: entry 95.715, best price afterwards 95.7525, exit "at" 95.906 for +Rs957.

Fix (`core/exits.lock_stop`, used by the live exits and all three backtests): the lock is capped at the trigger level, which price has
provably reached. `engine/trade_audit.py` now also flags any breakeven/trail exit beyond the best price after entry.

| Backtest | Before | After |
|---|---|---|
| USDINR (4 months) | 90 trades, PF 6.08, **+Rs31,465** | 84 trades, PF 2.58, **+Rs2,024** |
| EURINR | win 37.5%, PF 2.16, -Rs10.6k | win 9.1%, PF 0.53, -Rs17.8k |
| GBPINR | win 42.9%, PF 3.46 | win 10.0%, PF 1.08, -Rs6.6k |
| Equity (6 stocks) | PF 1.36, +Rs11.3k | PF 1.35, +Rs6.0k |
| CRUDEOILM / GOLDM / SILVER | - | identical (their 0.35% minimum stop keeps the trigger above the lock) |

**Currency's apparent edge was largely this artifact**; earlier currency tuning (including the 2026-09-19 win-rate re-tune) used the
inflated numbers. USDINR is now roughly break-even after costs; EURINR / GBPINR lose. Equity's edge roughly halves but stays positive.

## 2. Exit-structure sweep (one parameter at a time, current values as baseline)

With honest fills the **lock-buffer sweep is flat** (0.05% .. 0.40% all identical): the earlier "improvement" from larger locks was the
artifact above. Target multiple, trail width and time stop: current values are at or near the best. Two settings beat the baseline in both
halves, both on very few trades, neither adopted:

- earlier breakeven activation (0.4 instead of 0.6): pooled PF 1.56 -> 1.63 and 2.61 -> 6.91, but the second half is driven by 9 gold and
  7 silver trades with almost no losses;
- tighter trail (0.15 instead of 0.3): PF 1.56 -> 1.69 and 2.61 -> 2.76, smooth and monotone, but stops closer to price are more sensitive
  to slippage than the backtest assumes.

Worth a paper A/B if there is appetite; not enough evidence to change live constants.

## 3. Direction and regime

Long-only is worse everywhere (CRUDEOILM PF 0.69 -> 0.46 and 1.26 -> 0.92; GOLDM first half 0.89 -> 0.19): the shorts contribute. The
crude daily-regime gate changes nothing in this window (results identical with it on or off).

## 4. Other instruments

With the existing thresholds and 4 months of data: COPPER PF 0.66 (-Rs52k), NATGASMINI PF 1.0 (-Rs11k), ALUMINIUM 0 wins in 16 trades,
ZINC 11 trades, LEAD 3, NICKEL 8. None credible (most under the 15-trade bar); nothing to add.

## 5. Costs are most of the edge

| | Trades | Gross | Fees | Fees as % of gross |
|---|---|---|---|---|
| CRUDEOILM | 80 | +Rs19.4k | Rs18.9k | **98%** |
| GOLDM | 37 | +Rs23.7k | Rs19.4k | **82%** |
| SILVER | 33 | +Rs450k* | Rs107k | 24% |

*Margin sizing compounds silver's rupee figures on very few trades; do not read them as an expectation.

Crude and gold have a small gross edge that costs almost fully consume. Wider minimum stops (0.5%, 0.65%, 0.8% instead of 0.35%) do not
help (crude second half PF 1.26 -> 1.28 / 0.88 / 0.91; gold first half 0.89 -> 0.66 / 0.68 / 0.64).

## 6. Not done, and why

- **Runner deduplication.** An earlier estimate of "19 duplicated methods" counted names. Compared by body, `DryRunner` and
  `LiveTrader` share 13 methods and only 2 are identical (17 lines); 11 have genuinely diverged, and live trading is disabled by the kill
  switch. A merge would risk behaviour for almost no gain.
- **Risk sizing (4% per trade).** A decision for the account owner, not changed. With costs consuming most of the gross edge, lowering it
  shrinks losses and wins alike.
- **Fill offset vs the live price.** `engine/trade_audit.py` now reports the mean entry-fill offset in bps each night; read it after a few
  nights of clean 1-minute data.
