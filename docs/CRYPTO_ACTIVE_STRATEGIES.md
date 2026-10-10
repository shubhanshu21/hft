# Active crypto strategies: what survived (2026-10-06)

Question: the slow blend (engine/crypto_paper.py) trades a few times a month per coin. Is there a more active strategy that makes money
after fees? Searched hard: 7 families, 316 configurations on BTC/ETH/SOL perpetuals, 15-minute bars, fitted on 2021-2024 and judged only
on 2025-2026, with costs per side and 8-hourly funding (`markets/crypto/experiments/active_strategy_lab.py`; cross-sectional momentum on
9 coins, 1-hour bars, in the same study).

## Frequent trading loses after fees

| Family | Trades / coin / month | TEST 2025-26, best config |
|---|---|---|
| Hour-of-day timing | 30-90 | -15% to -69% a year |
| RSI-2 mean reversion (trend-filtered) | 30-60 | -19% to -38% |
| Bollinger squeeze breakout | 14-23 | -12% (9 bp) to +10% (5 bp) |
| Intraday time-series momentum | 6-14 | inconsistent, loses on TRAIN |
| Cross-sectional momentum (9 coins) | weekly | TRAIN up to +134%/yr, TEST -23% to +8% (decayed) |
| 15-minute channel breakout + ATR trail | 4-9 | best +8% (9 bp) / +15% (5 bp), worst drop 20-28% |
| **Daily volatility breakout, long-only, trend filter** | **~4** | **+5% (9 bp) / +9% (5 bp), worst drop 8-12%** |

## The one adopted: daily volatility breakout (markets/crypto/strategies/breakout.py)

Long once a 15-minute bar closes above today's UTC open + 0.7 x yesterday's range, if yesterday's close is above its 20-day average; flat at
the UTC day's end. At 6 bp/side, 1x: 2021 +37%, 2022 -3.5%, 2023 +42%, 2024 +19%, 2025 +5%, 2026 +12% (worst drops 7-13%). Neighbours hold
(k 0.7-1.0 positive on TEST; k 0.5 is not). Daily-return correlation with the slow blend: 0.32.

As an overlay on the blend (blend 1.5x + breakout 1x): TRAIN +43.3% -> +61.1% a year (Sharpe 1.19 -> 1.30, worst drop 45% -> 41%);
TEST +17.4% -> +25.2% (Sharpe 0.66 -> 0.76, worst drop 24% -> 25%).

Live: engine/crypto_breakout.py, inside the crypto daemon after the blend's cycle, real market orders on Binance's DEMO futures exchange,
own ledger var/db/crypto_breakout.db (`python3 -m engine.crypto_breakout --status`). The live decision is the last element of the same
`positions()` the backtest uses: 0 differences against the research code over ~205k bars per coin, and the decision computed on data up to
any moment equals the full-history value at that moment (900/900 random checks -- no look-ahead). The exchange holds one perpetual
position per coin for both sleeves; the blend subtracts this sleeve's open quantity and funding.

Fees decide most of these results: 9 bp -> 5 bp per side roughly doubles them. Binance USDT-M taker is 0.05% at the base tier; limit
(maker) orders at 0.02% would help a breakout entry only if the fill is not missed -- not tested.
