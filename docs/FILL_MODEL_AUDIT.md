# Fill-model audit: stops filled at prices that never traded (2026-10-07)

**Short answer.** Every simulator in this repo -- the three backtests, `core/exits.py` (paper trading), and `engine/exit_replay.py` --
filled a stop at the stop price even when the market had already moved past it. That made every strategy look much better than it was.
With fills at prices that really traded, the equity scalper loses in every year and is now **switched off**. SILVERMIC and USDINR stay
positive, but only with their chop gates, which were chosen on the same 4.5 months of data. CRUDEOILM and GOLDTEN stay stopped.

## What was wrong

1. **Stops placed past the market.** The trail sits close to the bar's extreme: equity and USDINR (dynamic exit) use 0.2-0.5 ATR from the
   bar's high, commodity uses 0.3 stop-distances. When a bar closes well off its high, the new stop is above that close (for a long). The
   next bar opens below the stop, yet the fill was the stop price. Example: HDFCBANK long 2026-05-14, stop raised to 774.48 by a bar that
   closed at 771.7; the next bar opened at 771.5; the backtest sold at 774.48. Across 2022-26, 41% of equity stop exits opened through the
   stop, by 22 bp on average. A round trip costs about 7 bp.
2. **Pullback entries dropped their worst trades.** A resting limit that a bar touched and then ran through the stop was treated as never
   filled. Price passes the limit on its way to the stop, so the order fills and is then stopped out: a full one-stop loss. That happened
   226 times in the equity backtest (`core/entry_pullback.py`).
3. **Target before stop.** When one bar reached both the take-profit and the stop, the commodity and currency backtests assumed the
   take-profit came first.
4. **Jumps inside a bar.** A 5-minute bar only shows that the stop was crossed. The 1-minute path can show a jump straight through it.
   Example: USDINR short 2026-07-03, stop 95.81; the 5-minute bar opened below the stop, and the next minute opened at 95.99.

The data itself is clean: a 5-minute bar opens within a median of 0-0.5 bp of the previous close, and the 1-minute archive matches the
5-minute bars exactly (same open, high and low).

## What changed

* `core/exits.py`: `stop_fill` (fill at the stop, or at the bar's open if it opened through it), `bar_exit` (stop first when one bar
  reaches both levels, unless the bar opened beyond the target), `intrabar_exit` (the same rules applied minute by minute). The live
  bar-sequence exits and the single-bar functions use them, so paper trading fills the same way.
* `core/minute_bars.py`: 1-minute candles grouped by 5-minute bar. The commodity, currency and equity backtests settle stop and target
  fills from them where the 1-minute archive exists. Commodity and currency have it for their whole history (topped up nightly); equity
  has it from 2026-05-12 (not topped up). Elsewhere the 5-minute rule applies. `minute_fills=False` switches this off for comparison.
* `core/entry_pullback.py`: `step` returns `stopped=True` for a fill-then-stop bar. The backtests record that trade as an `initial_stop`
  loss, and `PendingBook.check` returns `"stopped"`, which the paper engine records as an immediate stop-out.
* `markets/equity/scalping/backtest.py`: the CLI uses the live pullback entry (`EQUITY_PULLBACK_*` from `.env`) unless told otherwise.
* `tests/test_fill_model.py` covers the new rules; `tests/golden/replay_quick.json` was re-recorded (16 exits moved to the bar's open,
  one take-profit became a stop, and a 3% market cooldown cascade removed one entry; see `tests/test_strategy_parity.py`).

## Results, live settings (Rs1 lakh, SILVERMIC 10% risk / 7.9x, USDINR 10% / 20x dynamic exit, equity 4% / 5x / max 3 open)

| Segment | Before: n, win%, PF, net | After: n, win%, PF, net | Max DD after |
|---|---|---|---|
| SILVERMIC (live, chop gate 20) | 113, 65.5%, 1.38, **+48,142** | 113, 60.2%, 1.11, **+11,989** | 18.8% |
| SILVERMIC, no chop gate | 208, 61.5%, 1.19, +46,564 | 208, 56.2%, 0.90, -18,296 | 27.8% |
| USDINR (live, chop gate 65) | 39, 64.1%, 3.68, **+36,229** | 39, 38.5%, 1.57, **+13,588** | 9.4% |
| USDINR, no chop gate | 92, 55.4%, 1.70, +35,267 | 92, 37.0%, 0.89, -6,290 | 18.2% |
| CRUDEOILM (stopped) | 195, 66.2%, 1.17, +15,678 | 223, 49.8%, 0.51, -43,149 | 43.1% |
| GOLDTEN (stopped) | 96, 55.2%, 1.07, +7,824 | 96, 54.2%, 0.94, -5,669 | 26.9% |
| Equity, 49 names, 2022-26 (live pullback entry) | 2,395, 65.6%, 1.32, **+6,63,412** | 2,578, 51.0%, 0.75, **-7,02,362** | account lost |

Equity by year, after: 2022 -63k, 2023 -158k, 2024 -298k, 2025 -54k, 2026 -129k. The average trade earns -Rs15 before costs and pays
Rs258 in costs. A Rs1 lakh account is never more than +Rs7.7k up and is gone by 2023-08-02 (610 trades). Commodity and currency cover
2026-05/06 to 2026-10 only (the length of Upstox's archive), so their samples are small.

The 1-minute replay of the same entries, done independently, agrees with the fixed backtests: SILVERMIC +12.7k, USDINR +13.7k.

## Exit-free check: does the equity entry signal predict anything?

Signed return in the trade's direction, 5-80 minutes after each signal bar's close (3,809 signals, no exits involved):

| Period | 15 min | 60 min | 80 min |
|---|---|---|---|
| 2022-23 | +0.3 bp | +12.9 bp | +15.4 bp |
| 2024 | -3.4 bp | +0.1 bp | -1.4 bp |
| 2025 | -1.2 bp | +1.2 bp | +0.1 bp |
| 2026 | -1.9 bp | -0.4 bp | -2.9 bp |

Against a round-trip cost of about 7 bp, the signal has had no edge since 2024. No exit design can turn a signal with no edge into
profit, so the equity scalper was switched off rather than re-tuned (`ENABLE_EQUITY_TRADING=false`, `DRYRUN_INCLUDE_EQUITY=false`).

SILVERMIC (+6 to +10 bp at 15-80 minutes against about 4.4 bp of cost) and USDINR (+2 to +2.5 bp against about 1.9 bp) do show small
positive edges, and not just trend-riding: silver fell 20% over the window while its long signals did best. The margin is thin, and it
exists only with the chop gates. The USDINR gate was chosen after the TEST window lost at every leverage, which spent that window. Treat
both as unvalidated until new data arrives, and make no further tuning on the existing archive.

## What is still approximate

* **Paper trading** fills a stop at the stop price when price crosses it between two 30-second scans. It sees 5-minute candles only, so
  it cannot tell how far a jump went. The cost model's half-spread slippage covers normal crossings; news jumps like USDINR 2026-07-03 are
  not covered.
* **No tick rounding.** Stops are not rounded to the tick grid (USDINR 0.0025), which is worth about Rs20 a trade at 19 lots.
* **Earlier documents** quote pre-fix numbers, including `EDGE_AUDIT.md`, `LIVE_VS_BACKTEST_EXITS.md`, the chop-gate commits and the
  README's equity fold table. The random-entry comparisons in `EDGE_AUDIT.md` used the same flawed fills on both sides, so their
  rankings may still hold, but their rupee figures do not.
