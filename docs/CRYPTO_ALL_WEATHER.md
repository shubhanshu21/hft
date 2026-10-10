# Crypto that works in bull, bear and sideways markets (2026-09-28)

## CORRECTION (same day): what the deployed logic actually earns
The research tables further down pick each regime's best SLEEVE as if switching between them were free (`all_weather_study.py`), which overstated the router (+28% / Sharpe 1.02 test). `markets/crypto/backtest.py`
runs the exact live logic -- the router's signed targets, the trader's 10% band, spot and perp costs, funding on shorts, position changes when the regime flips -- and gives the honest numbers below.
Router alone: WHOLE +23% CAGR / Sharpe 0.86 / max DD 44%; TRAIN +20% / 0.76 / 44%; TEST +26% / 0.96 / 22%; years +39, +21, -1, +50, +2, +23%. It shorted into the early-2023 rebound (-1% vs +49% for trend alone) and its 2021-23 drawdown was larger.
Requiring a regime to persist for 2-20 days before switching did not fix that (train 12-22%). What did: **BLEND = half the capital on the router, half on the plain trend ensemble** (`python3 -m markets.crypto.backtest --router-weight 0.5`):
| Book (live logic) | TRAIN 2021-03..2023: CAGR / Sharpe / max DD | TEST 2024..now | WHOLE | Worst year | Last 12 months |
|---|---|---|---|---|---|
| Buy and hold | +46% / 0.87 / 86% | +17% / 0.57 / 65% | +31% / 0.74 / 86% | -79% | -32% |
| Trend only (deployed earlier on 09-28) | +20% / 0.94 / 37% | +20% / 0.92 / 25% | +20% / 0.93 / 37% | -24% | +0.1% |
| Router alone | +20% / 0.76 / 44% | +26% / 0.96 / 22% | +23% / 0.86 / 44% | -1% | +14.0% |
| **BLEND 50/50 (deployed)** | **+22% / 0.97 / 32%** | **+24% / 1.03 / 19%** | **+23% / 1.00 / 32%** | **+0.2% (2022)** | **+9.2%** |
Blend by year: 2021 +45.0%, 2022 +0.2% (trend alone -24%), 2023 +21.5%, 2024 +50.3%, 2025 +2.3%, 2026 +17.6% (buy and hold +260 / -79 / +293 / +92 / -14 / -6): 10,000 USDT -> 31,913 (500 -> 1,596). Test-period quarters:
+48.2, -9.7, -4.4, +17.5, -3.6, +1.5, +14.1, -8.4, +8.4, +0.5, +7.8 (all but four quarters beat buy and hold's downside; it lags in strong rallies). By BTC regime (annualised): bull +48% (Sharpe 1.72), bear -4%, sideways -2% --
so it does not EARN in bear or sideways, it roughly breaks even there (buy and hold: -45% / -110%), and it is positive in every calendar year. Honest limits: the bear-market evidence is thin (2022, 2025-26), perp margin / liquidation risk is not modelled, taxes are not modelled.

Request: a strategy for BTC / ETH / SOL that works in bear, bull and sideways markets (not only when crypto goes up). Research: `markets/crypto/experiments/all_weather_study.py` (hourly Binance data 2020-09..2026-09 incl. perpetual funding rates,
TRAIN 2021-03..2023-12 / TEST 2024-01..now, all costs charged: spot 0.12% / SOL 0.15% per side, perp 0.09% per side, funding received / paid).

## Regimes (BTC, past data only): BULL = close above the 200-day EMA and the 50-day EMA above it; BEAR = both below; SIDEWAYS = everything else (52% / 33% / 14% of the time). A stricter 30-day trend-efficiency label (below 0.20 = range-bound: 56% of the time) was tested too.
## What each tool earns in each regime (annualised return while the regime is on; whole period)
| Sleeve | Bull | Bear | Sideways (EMA label) | Sideways (efficiency label) |
|---|---|---|---|---|
| Buy and hold | +159% | -45% | -110% | +32% |
| TREND (slow long/flat ensemble) | **+48%** | -15% | +5% | +13% |
| SHORT (perp short in bear, sized by the trend signals off) | -10% | **+8%** (test +16%) | +12% | -3% |
| CARRY (long spot + short perp, earns funding) | +11% | -5% (0 when gated) | +4% | +4% |
| REVERT (mean reversion inside a range) | -1% | -0% | **-5%** | -1% |
Findings: (1) the slow trend ensemble is the bull engine and also earns in ranges (+5..13%); (2) the only sleeve that earns in a BEAR is the short overlay (+8%/yr, test +16%, and +48% in the strictest trend-down label);
(3) mean reversion does NOT work in crypto sideways markets (negative in every label and both periods -- same as the earlier scalping research); (4) funding carry is direction-neutral but its yield has decayed
(BTC funding paid shorts 17% in 2020 and 31% in 2021, 5.1% in 2025, 2.2% in 2026 so far; SOL was negative in 2020 and 2022 and ~0 in 2025-26) and needs a futures account with margin.

## Books compared (TRAIN CAGR / Sharpe / max DD  |  TEST)
| Book | TRAIN | TEST | Worst calendar year |
|---|---|---|---|
| Buy and hold | +46% / 0.87 / 86% | +17% / 0.57 / 65% | -79% |
| TREND alone (what was deployed on 09-28) | +20% / 0.94 / 37% | +20% / 0.92 / 25% | -24% (2022) |
| **ROUTER: bull TREND / bear SHORT / sideways slow TREND** | **+28% / 0.99 / 37%** | **+28% / 1.02 / 22%** | **+2% (2025)** |
| ROUTER with sideways in cash | +29% / 1.06 / 40% | +26% / 0.97 / 23% | +6% |
| 70% router + 30% carry | +22% / 1.10 / 27% | +21% / 1.08 / 15% | +3% |
| 50% router + 50% carry | +18% / 1.23 / 19% | +17% / 1.16 / 11% | +2% |
| Router with mean reversion in sideways | worse than cash | -2% in sideways | |
Router by calendar year: 2021 +45%, 2022 +24%, 2023 +12%, 2024 +50%, 2025 +2%, 2026 +28% (buy and hold +260% / -79% / +293% / +92% / -14% / -6%): positive in every year. By regime (EMA label): bull +48%, bear +8%, sideways +5% -- positive in all three.

## What is deployed (paper): the BLEND (half router, half trend; `CRYPTO_STRATEGY=blend`, `CRYPTO_ROUTER_WEIGHT=0.5`; `router` and `trend` remain selectable)
(The description below is the router half.)
`markets/crypto/strategies/tf_1hour/router.py`, run by `engine/crypto_paper.py` (`CRYPTO_STRATEGY=router`). Bull or sideways BTC regime: long spot, size from the slow-momentum ensemble; bear regime: short perpetuals (funding accrued every 8 hours), larger the fewer trend signals are on,
only for coins that are themselves in a bear regime. Carry is NOT included (its yield has decayed to ~2%; add it later if funding recovers: 30-50% carry cut the drawdown to 11-15% for 2-11 points of return).
Caveats: the short sleeve's evidence is thin (one deep bear market, 2022, plus 2025-26); regime labels lag turning points; perpetual shorts add liquidation / margin risk that the paper account does not model; funding is applied to the position held at each 8-hour mark; taxes not modelled.
Today's regime: BULL for BTC, ETH and SOL (16-21 days in), so the router holds the same long positions as the trend-only trader (BTC 1.00, ETH 0.88, SOL 0.73).

## Leverage: 1.5x chosen (2026-09-28)
Hybrid venue split as executed (spot long up to 1x a coin's share; shorts and the part of a long above 1x are perpetuals; the excess long pays funding), blend strategy, Mar 2021..now, 10% band, costs + funding:
| Leverage | CAGR (train / test) | Sharpe | Max DD | Calmar | Worst 12 months | Since 2025 | Growth if the real edge is 70% / 50% of the backtest | Worst day |
|---|---|---|---|---|---|---|---|---|
| 1.0x | +23% (22 / 24) | 1.00 | 32% | 0.73 | -23% | +20% | +15% / +9% | -7.1% |
| 1.25x | +29% (28 / 31) | 1.02 | 37% | 0.78 | -28% | +23% | +18% / +11% | -8.8% |
| **1.5x** | **+34% (33 / 36)** | 1.01 | 44% | 0.78 | -34% | +29% | +21% / +12% | -10.5% |
| 1.75x | +39% (38 / 41) | 1.01 | 50% | 0.78 | -39% | +34% | +23% / +13% | -12.1% |
| 2.0x | +43% (42 / 45) | 1.00 | 56% | 0.77 | -44% | +35% | +24% / +13% | -13.8% |
| 3.0x | +55% (52 / 57) | 0.97 | 74% | 0.73 | -61% | +37% | +26% / +10% | -20.0% |
Reasons for 1.5x: Calmar (return per unit of drawdown) is flat from 1.25x to 1.75x and falls after; Kelly (mean/variance) is 4.2x if the backtest edge is fully real, 3.0x at 70% of it, 2.1x at 50%, so half-Kelly is 1.1-2.1x; beyond 1.75x the
growth if the edge is only half real stops improving (+13% at 1.75x and 2x, +10% at 3x) while the worst drop and the worst rolling year keep growing; 3x has a 74% max drawdown and a -20% worst day. Isolated-margin liquidation simulation (hourly highs / lows): no liquidation at any of
1x-3x in 5.6 years, the closest call used 28% of the 3x liquidation distance -- but the data contains days (SOL -42% on 2022-11-09, ETH -28% on 2021-05-19) that a full-size 3x long would not survive, and the strategy was short or lightly sized then.
If ALL exposure is put on perpetuals instead (longs pay funding on the whole notional): 1.0x +19% / Sharpe 0.87, 1.5x +28% / 0.87 / 46% DD -- about 4-5 points a year lower, which is why longs stay on spot up to 1x.

## Execution on Binance's DEMO exchange (2026-09-28)
`services/broker/binance_demo.py` sends real MARKET orders to `demo-api.binance.com` (spot) and `demo-fapi.binance.com` (USDT-M futures) with demo keys (created at demo.binance.com -> API Key Management, separate from production keys); it refuses every other host.
`CRYPTO_EXECUTION=auto` uses the demo exchange when `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` are set, otherwise our own simulation. The exchange is the source of truth for holdings (our quantity = its balance minus the baseline recorded at reset);
the sizing ledger (equity at CRYPTO_CAPITAL_USDT) follows the real fills, real fees and the demo account's own funding payments. Exchange minimums (measured 2026-09-28): spot 5 USDT; futures BTC 50, ETH 20, SOL 5 USDT: orders below them are skipped and logged.
Isolated margin, perpetual leverage setting 2x. Checks: `python3 -m engine.crypto_paper --demo-check` (read-only) and `--demo-smoke` (tiny buy/sell and short/cover on the demo exchange), then `--reset` to record the baseline and restart `hft-crypto`.

## More coins? (2026-09-28) - no, the three we trade are already as good as it gets

Question: would BNB, XRP, ADA, DOGE, LINK or AVAX add profit? Same live blend logic (BTC-anchored regime router 50% + trend 50%), 1.0x, equal weights, real costs (alts 0.15%/side) and perp funding, 2021-04-10 to 2026-09-28 (`markets/crypto/experiments/coin_universe_study.py`).

| Portfolio | Per year | Sharpe | Train (to 2023) per year | Test (2024+) per year |
|---|---|---|---|---|
| BTC ETH SOL (live) | +21.0% | 0.93 | +17.5% | +24.6% |
| + BNB | +19.9% | 0.91 | +12.4% | +27.8% |
| + XRP | +17.6% | 0.85 | +10.0% | +25.7% |
| + ADA | +19.4% | 0.91 | +17.4% | +21.5% |
| + DOGE | +22.7% | 1.03 | +18.5% | +27.0% |
| + LINK | +16.9% | 0.80 | +12.4% | +21.7% |
| + AVAX | +21.7% | 0.99 | +22.6% | +20.9% |
| all 9 | +17.5% | 0.89 | +12.1% | +23.1% |

Only DOGE is better in both windows, by +1.7%/yr and +0.10 Sharpe - within noise, and DOGE's own history (meme coin) makes it a survivorship pick: the coins were chosen because they exist and are large today. XRP and LINK alone lose money in the train window. All 9 coins is worse than 3. Correlations between coins' blend returns are 0.4-0.7, so diversification is real but too small to pay for the weaker signals. Decision: keep BTC/ETH/SOL.
