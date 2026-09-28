# Live indicators now see the previous sessions (fixed 2026-09-28)

Symptom: no trade all morning (2026-09-28 until 11:10 IST). The live scanner gave the rule only TODAY's 5-minute bars (Upstox's intraday endpoint returns nothing older), while every backtest computes EMA / ADX / RSI over continuous multi-day
history. With 14-26 bars the indicators are still warming up and differ from what was validated: ADANIENT at 10:20 -- backtest ADX 51.0, EMA slope -0.187, RSI 18.9 (rule fires: short); live-style ADX 74.7, slope -0.171, RSI 27.4 (rule silent).
Over the whole NIFTY universe the backtest-style replay produced 1 entry by 11:05 (ADANIENT short 10:20) and the live-style replay produced none; commodity and USDINR had none either way.
Also seen in the logs: "[X] Series has 26 rows but indicator requires at least 45" on every scan (one per commodity/currency symbol) -- gone after the fix.

Fix (engine/live_dryrun.py `_fetch_candles` / `_previous_sessions`): the previous sessions (LIVE_CANDLE_HISTORY_DAYS calendar days, default 6, 0 = off) are fetched ONCE per symbol per day with one historical-candle call and placed in front of today's bars.
Checked: ADANIENT now gets 323 bars and the live-style rule fires the same 10:20 short. Cost: ~53 extra calls once per day (plus once after any restart). Tests: tests/test_currency_dynamic_exit.py::TestLiveCandlesIncludePreviousSessions.
Earlier live results (the 2026-09-24/25 trades, the pullback and dynamic-exit decisions) were taken with today-only indicators; expect live signal counts closer to the backtest's (median ~2 equity entries a day) from now on.
