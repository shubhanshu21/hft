# Crypto: slow momentum on BTC, ETH and SOL (Binance), paper trading (added 2026-09-28)

Request: trade crypto on 15-minute and 1-hour bars to capture long-term momentum, BTC + SOL + ETH, using Binance. The project's earlier crypto scalping was removed (README scope note: no out-of-sample edge in 650+ fast strategy combinations),
so this was researched first with the project's discipline: variants fixed in advance from the literature, TRAIN 2020-08..2023-12 (2021 bull run + 2022 bear market), TEST 2024-01..2026-09 untouched, costs charged on every position change
(0.12% per side BTC/ETH, 0.15% SOL = taker fee 0.10% + slippage), against buy-and-hold. Data: Binance public spot klines, ~6 years, 15m and 1h, `python3 -m services.data.binance` (var/archive/crypto/, ~800k candles).

## Results (`markets/crypto/experiments/momentum_study.py`, equal-weight portfolio of the three coins)
| | TRAIN CAGR / Sharpe / max DD | TEST CAGR / Sharpe / max DD |
|---|---|---|
| Buy and hold (equal weight) | +122% / 1.36 / 86% | +17% / 0.57 / 65% |
| Fast momentum (7-14 day lookbacks), long/flat | +46..79% / 0.96..1.33 | -13%..+6% / -0.21..0.35 (loses) |
| ANY long/short version (perps) | mostly negative | negative to ~0 (loses) |
| Slow, long/flat, volatility-targeted: Donchian 20/10d, EMA 10/30d, EMA 20/50d, EMA 50/200d, TSMOM 90d | +26..44% / 1.05..1.59 / 27..50% | +16..22% / 0.69..1.03 / 24..35% |
Slow trend rules (three unrelated families: channel breakout, moving-average cross, past return) beat buy-and-hold on Sharpe AND drawdown in the held-out test, on both the 1-hour and the 15-minute bars: 21 of 80 variants pass (11 on 1h, 10 on 15m),
all of them long/flat and slow. Going to 15-minute bars adds nothing: with lookbacks of weeks the position changes only 2-14 times a year at either resolution; faster lookbacks just lose more to costs.

## What is traded: the ensemble (`markets/crypto/strategies/momentum.py`)
Average of EMA 20/50-day crossover, Donchian 20-day breakout / 10-day exit and 90-day time-series momentum (each 0/1) x a volatility scale min(1, 40% / annual volatility of the previous 30 days). Spot, never leveraged, never short.
Per asset, per calendar year and per period (`ensemble_study.py`): TEST portfolio +19% CAGR, Sharpe 0.87, max DD 26% (buy-and-hold +17%, 0.57, 65%); BTC 0.91 vs 0.76, ETH 0.84 vs 0.41, SOL 0.58 vs 0.48.
It is a RISK-CONTROLLED exposure, not extra alpha: it gives up most of a bull run (2021: +92% vs +1,132% buy-and-hold; 2023: +46% vs +293%; 2024: +46% vs +92%) and wins when crypto falls or stalls (2022: -24% vs -79%; 2025: +3% vs -14%; 2026: +7% vs -6%).
Limits: one and a half market cycles of data (2020-2026), funding and exchange risk not modelled, taxes not modelled (India: 30% on gains + 1% TDS on transfers).

> Update 2026-09-28 (later): the paper trader now runs the REGIME ROUTER (long in bull/sideways, short in bear) described in docs/CRYPTO_ALL_WEATHER.md; the long/flat ensemble below is its bull/sideways engine (`CRYPTO_STRATEGY=trend` still runs it alone).

## Paper trader (`engine/crypto_paper.py`, systemd `hft-crypto.service`)
PAPER ONLY -- Binance public klines, no API key, no orders. Wakes every 15 minutes; when a new hourly bar has closed it decides each coin's target size and rebalances at the latest 15-minute price with the backtest's cost. Each coin gets a third of the equity;
it trades only when the target differs from the holding by more than 10% of that share (`CRYPTO_REBALANCE_BAND`) or the target is zero. Own SQLite (`var/db/crypto_paper.db`, 10,000 USDT start), Telegram alert on trades,
`python3 -m engine.crypto_paper --status | --once | --reset --capital N`. Started 2026-09-28 08:37 UTC: bought BTC / ETH / SOL at targets 1.00 / 0.88 / 0.73, equity 9,988.84 USDT after entry costs.
Tests: tests/test_crypto_paper.py.
