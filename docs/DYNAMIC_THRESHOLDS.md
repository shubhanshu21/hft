# Fixed entry thresholds vs thresholds that follow the market (run 2026-09-26, `dynamic_thresholds_study.py`)

Request: "I don't want static things, I need dynamic things."

## What is already dynamic
Position size (equity x stop distance x Upstox's real margin, scaled down in a drawdown), stops (1.4 x current ATR, floored at 0.35%), the trailing exit (current-bar ATR each bar), the pullback depth (a fraction of the current stop),
margin, lot size, tick size, session hours, holidays, costs (measured spreads once sampled), token/API budget. Static: the ADX / volume-surge / EMA-slope / VWAP / ORB entry levels, the 80-minute time limit, the 0.6R break-even level, the risk % and the
drawdown tiers (5 / 10 / 15% -> 10 / 25 / 50% smaller).

## Test: entry thresholds that follow the market (opt-in `adaptive_window` in markets/commodity/strategies/tf_5min/scalping/backtest.py, off by default)
ADX, volume surge and |EMA slope| each compared with their own rolling quantile over the previous 1 / 3 / 5 / 10 trading days, at the quantile the fixed number represents over the whole sample (same selectivity, moving level).
Net Rs, real MCX first / second half | proxy 2024 / 2025 / 2026 (fixed vs the best-looking window):
| Symbol | Fixed (live) | Dynamic (best window) |
|---|---|---|
| SILVERMIC | +21,964 / +23,958 | 10 days: +45,400 / +24,252 |
| | -90,077 / -83,605 / +74,236 | 10 days: -90,046 / -87,061 / +68,757 |
| GOLDTEN | -959 / +18,505 | 3 days: +8,033 / +15,966 |
| | -64,733 / -16,463 / +39,440 | 3 days: -49,045 / -10,512 / +112,043 |
| CRUDEOILM | -16,539 / +16,346 | 3 days: -8,946 / +8,573 |
| | -38,764 / -58,468 / +66,960 | 3 days: -35,096 / -67,232 / +80,276 |
| NATGASMINI | -17,278 / +3,597 | 5 days: -11,018 / +724 |
Verdict (beat fixed in BOTH real halves and >= 2 of 3 proxy years): none. Each window helps one period and hurts another; silver 10 days beats both real halves but not the proxy; GOLDTEN 3 days beats 5 of 6 periods but
gives up 2.5k in the second real half. Consistent with the earlier regime-switching and gate tests: making the rule adaptive did not, on this data, beat a fixed rule. Not deployed.
