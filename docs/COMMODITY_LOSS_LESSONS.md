# Do the lessons from the losing trades fix anything? (run 2026-09-25, `loss_lessons_study.py`)

Lessons (from docs in this folder and the crude/silver/gold loser analysis): (1) 64-79% of losers never reach 0.3R -- they reverse at once; (2) costs decide viability (crude gross Rs148/trade < fees Rs169).
Tests, as opt-in parameters of markets/commodity/scalping/backtest.py (defaults = live behaviour, unchanged):
- `confirm_bars` 1 / 2: on a signal, wait and enter only if the breakout held (no bar back through half a stop distance; last close still beyond the signal close).
- `max_cost_r` 0.10 / 0.15 / 0.20: skip a signal whose round-trip costs exceed that fraction of the risked amount.
Judged on the real MCX archive in two halves and on the 2.7-year global-price proxy by calendar year (capital restarts each year); "improves" = beats baseline net in BOTH real halves AND >= 2 of 3 proxy years.

Result: **no variant improved any symbol.** Selected numbers (net Rs, real first half / second half | proxy 2024 / 2025 / 2026):
| Symbol / variant | Real | Proxy |
|---|---|---|
| CRUDEOILM baseline | -16,539 / +16,346 | -38,764 / -58,468 / +66,960 |
| CRUDEOILM confirm 1 bar | -6,275 / -16,858 | -41,611 / -19,345 / -2,141 |
| CRUDEOILM cost gate 0.20 | -7,392 / +10,603 | +11,425 / -13,539 / +91,462 |
| SILVERMIC baseline | +21,799 / +23,813 | -90,057 / -82,920 / +67,666 |
| SILVERMIC confirm 1 bar | +45,535 / +17,781 | -80,714 / -50,416 / +414,479 |
| GOLDTEN baseline | -50 / +18,817 | -63,494 / -14,872 / +42,907 |
| GOLDTEN confirm 1 bar | +22,220 / +6,090 | -51,653 / -26,573 / +24,039 |
| NATGASMINI baseline | -17,278 / +3,597 | -28,682 / -15,160 / +403 |
| NATGASMINI confirm 1 bar | -4,226 / +3,226 | -13,825 / +7,767 / -5,599 |

Reading: confirmation halves the trade count and helps in the weak periods (silver first half +45.5k vs +21.8k; gold first half; natgas both 2025), but gives back the strong trend periods (crude 2026 +67.0k -> -2.1k; silver and gold second half). Each fix
swaps one regime's result for another's; none beats the baseline in both real halves. The cost gate mostly just trades less: on silver it excludes almost nothing on real data (identical to baseline), and on crude it improves the first half but cuts the second.
The closest call is silver + 1-bar confirmation (better in the first real half and in all three proxy years, worse by Rs6k in the second real half); the proxy's 2026 +414k comes from compounding inside a strongly trending year, so it is an outlier, not evidence.
Not deployed. Next useful step is more real MCX history (the nightly job adds ~1 month/month) or forward paper results, then re-run: `python3 -m markets.commodity.experiments.loss_lessons_study`.
