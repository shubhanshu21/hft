# Why the live paper account lost money while the backtests made money (2026-10-05)

> **2026-10-07:** the rupee figures below come from the old fill model, which filled stops at prices the market had already passed. See [FILL_MODEL_AUDIT.md](FILL_MODEL_AUDIT.md) for the corrected numbers; equity is now off.

**Short answer:** the live engine managed exits differently from every backtest, and the difference cut winning trades short. Same
entries, exits replayed at 1-minute resolution: commodity + currency went from **+Rs63,012** (backtest mechanics) to **-Rs57,282**
(live mechanics); equity from **-Rs30,678** to **-Rs71,541** over its own recent window. Fixed in `core/exits.py` (`*_bars` functions).

## What was wrong

Upstox's intraday candle API returns the **still-forming** 5-minute bar as its newest candle (measured 2026-10-05 23:24 IST: the
23:20 bar's volume grew 971 -> 1,280 -> 1,443 as each minute completed; a minute's bar appears ~15-20 s after it ends). The live
engine scans every ~30 s and handed that forming bar to the exit functions every time:

1. scan k of the bar sees the bar's high so far -> arms the breakeven / raises the trail;
2. scan k+1 of the **same** bar tests the bar's low so far -- often made minutes **before** that high -- against the raised stop;
3. the trade closes at a price that never traded after the stop moved.

The backtests cannot do this: they test bar i's adverse extreme against the stop as it stood before bar i, and only then move the stop
with bar i's favourable extreme (it applies from bar i+1). The golden replay (`tests/replay_harness.py`) never caught it because it
scans once per completed bar, exactly like a backtest.

Live evidence before the fix: winners exited 1.5-6 minutes after entry (CRUDEOILM 1.6 min, USDINR 3.4-4.9 min, UPL 3.8 min) while
losers ran 27-69 minutes to the full stop. 7 of 16 moved-stop exits never traded at their exit price after the best price that moved
the stop; UPL kept falling 0.9% after a short was closed, crude 0.86%, ASIANPAINT 0.34%.

## Measured cost: same entries, three exit mechanics (`python3 -m engine.exit_replay`)

The backtests' own entries (live settings: chop gates, USDINR dynamic exit at 20x), 1-minute archive. `bar` = backtest logic,
`old_live` = the pre-fix live engine, `live` = the fixed live engine (checks the stop every scan, moves it only from completed bars).

| | bar (backtest) | old_live | **live (fixed)** |
|---|---|---|---|
| SILVERMIC (112 trades) | +37,245 | -11,420 | **+31,127** |
| CRUDEOILM (291) | -12,391 | -43,385 | **-16,596** |
| GOLDTEN (95) | +6,966 | -20,950 | **+6,966** |
| USDINR (39) | +31,192 | +18,472 | **+31,192** |
| commodity + currency (537) | +63,012 | -57,282 | **+52,689** |
| equity, 49 names, 2026-05-12..10-01 (169 pooled) | -30,678 | -71,541 | **-36,666** |

Average winning trade, commodity + currency: +1,126 backtest, +740 old live, +1,094 fixed -- losses identical (-1,370), so the whole
gap is winners being cut short. The fixed engine is a little below `bar` where TP / stop / timeout order inside a bar is now resolved
minute by minute instead of assumed -- that is the more realistic of the two.

Moving the stop every minute with correct ordering (only post-move prices may hit it) recovered only part (+13,262 for commodity +
currency): the validated edge depends on the trail being advanced once per 5-minute bar.

## Validation on the actual live trades

The same 1-minute replay applied to the 24 real paper trades of 2026-09-25..10-01 (the 3 trades of 10-05 had no 1-minute history
yet): the OLD exit code reproduces what really happened -- same exit reason on all 24, total **-Rs14,089 vs -Rs14,094 actual** -- so
the simulator is faithful. The FIXED code on those same 24 trades: **-Rs11,912**, i.e. +Rs2,182 (two crude shorts reach their
take-profit, a USDINR short and two winners run longer). Most of that account's loss was genuine full-stop losers (four SILVERMIC
whipsaws -10.9k, LT and TATASTEEL -5.5k) that no exit change touches: the bug costs ~Rs220 a trade on a normal mix of winners, and
those 24 trades happened to be loser-heavy.

## What was NOT wrong: entries on the forming bar

Live also evaluates **entries** on the forming bar. Tested with the live entry function on 1-minute data (same opportunities, a fixed
1.8R / 1R / 80-minute exit for both): entering at the first scan that signals made +37.3R vs +18.9R waiting for the bar close on
SILVERMIC (177 vs 162 trades) and +15.9R vs +15.1R on USDINR. Signals that vanish by the close are rare (15 of 177) -- left as is.

## Also fixed on 2026-10-05

* **Live short rules never matched the backtests.** `entry_signal.py` required ADX +3, slope x1.2, ORB/VWAP x1.1, volume x1.25 for
  shorts and used TP x0.8 with a 0.4R breakeven. Added to the backtests as `live_short_rules` and run on the real archive: TEST
  (2026-08-01..10-01) +31,084 vs +43,531 symmetric across SILVERMIC/CRUDEOILM/GOLDTEN/USDINR; TRAIN +18,205 vs +20,491. Live now uses
  the symmetric, validated rules (and currency levels are rounded to 4 decimals, not 2).
* **A restart un-armed positions.** The entry-time state (`armed_be: False` / `armed_trail: False`) overwrote the armed flag reloaded
  from the database, so a restarted position stopped trailing. The flag now comes from the `armed_be` column, and the last bar applied
  (`trail_bar_ts`) is persisted with each stop update.

## Not adopted

* NIFTY daily-autocorrelation gate for equity (`USE_EQUITY_REGIME_FILTER`): every variant (window 10/15/20, either sign) roughly halved
  trades and net profit on both TRAIN (2022-08..2024-12, +381k ungated) and TEST (2025-01..2026-10, +272k ungated) without a consistent
  quality gain, and did not reliably remove the 2026-05..09 losing stretch. Off.

## Still true

* Equity's own backtest lost money May-September 2026 (-38k, PF 0.82) after profitable years (2023 +2.39 lakh, 2025 +1.79 lakh); it
  is a drawdown in a strategy with a 4-year edge (TRAIN PF 1.28, TEST PF 1.38), not something an exit fix changes.
* CRUDEOILM is negative in the backtest itself on the real archive (TRAIN -15.6k, whole period -12.4k with the regime gate).
