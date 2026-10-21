# markets/ -- market -> timeframe -> strategy

```
markets/
  <market>/                          currency | equity   (commodity/: MCX data download only, no strategies)
    costs.py  data.py  features.py   shared by every strategy of that market (cost model, data top-up, indicators)
    strategies/
      tf_5min/                       strategies whose signal uses 5-minute bars
        <name>/                      one folder per strategy -- as many as you like per timeframe
          strategy.py                the Strategy class (core/strategy.py), exposes STRATEGY -- found automatically
          backtest.py                its backtest, reusing the same entry code
          ...                        anything else it needs (entry_signal.py, signal.py, proxy.py)
        <another name>/
      tf_15min/  tf_1hour/  tf_daily/
    experiments/                     throwaway research scripts, never run in production
```

Add a strategy: `python3 cli.py new-strategy --market currency --timeframe 15min --name my_idea` creates
`markets/currency/strategies/tf_15min/my_idea/strategy.py` from a template (timeframes: `5min`, `15min`, `1hour`, `daily`). The engine
discovers it by itself (`core/registry.py`); it only trades once named in `.env`, e.g. `CURRENCY_STRATEGIES=my_idea` (and
optionally `CURRENCY_MY_IDEA_SYMBOLS=...` to limit it to some symbols). Names must be unique within a market, whatever the timeframe.
A test (`tests/test_strategy_framework.py`) fails if a strategy sits in a folder that does not match its timeframe (`timeframe`, or
`signal_timeframe` when the signal uses other bars than the ones the runner fetches). Guide: `docs/ADDING_A_STRATEGY.md`. Project
rule: a credible backtest (>= 15 trades) and a held-out test window before it is switched on, then paper trading.

## Strategies (2026-10-15; paper trading only -- no real orders anywhere)

Crypto (the Binance paper trader and its strategies) and the MCX `gold_crude` strategy were removed on 2026-10-14 (user: "remove commodity and
crypto folders"; git history has them). MCX DATA IS STILL DOWNLOADED (user: "keep mcx nightly jobs ... for downloading data"):
`commodity/data.py` (nightly "mcx data") and `commodity/daily_series.py` (nightly "commodity daily series"); the order book is recorded by
`engine/depth_recorder.py`. MCX is TRADED AGAIN (paper) since 2026-10-15 by `commodity/trend_carry` only, on real MCX month-end
bhavcopies the user downloaded (2021-12 -> 2026-10) plus the nightly every-expiry top-up `commodity/futures_eod.py` (nightly "mcx futures eod",
var/archive/commodity/mcx_futures_daily.csv). Earlier deletions: the 5-minute scalpers (2026-10-10), the EURINR/GBPINR parity scalp and the crypto daily breakout (2026-10-12).
Crypto's `trend_breakout` and `calm_coins` (and the `CRYPTO_CALM` account) were replaced 2026-10-20 by a single `compounder_5lakh` (1-hour, BTC/ETH/SOL),
which was itself replaced 2026-10-21 by `dip_reversal` below (user: "if its better and giving more profits then remove previous and add this") --
screened from ~60 public freqtrade strategies on our own coin data with real costs; `dip_reversal` ranked above `compounder_5lakh` on the same
2022-26 data. The user's standing rule since then: when a better strategy is found, compare on the same cost model and delete the old one.

| market | folder | what it does | status |
| crypto | `crypto/strategies/tf_5min/dip_reversal/` | 5-minute dip-buy reversal on 9 coins' perpetual futures (BTC ETH SOL BNB XRP DOGE ADA AVAX LINK): a Bollinger-band squeeze close (BinHV45) or a slip under the 20-candle lower band below the 50-candle EMA (ClucMay2018) -> long; exit at a 7% target (limit), a 7% stop, the close back above the 20-candle mid-band, or a 24h time stop (our own safety cap). 2% risk per trade, up to 3 positions open together (shared margin pool); taker fee 0.05%+18% GST+slippage on entries/stops, maker fee 0.02%+GST on the target, real funding, gapped stops at the open. Rs1L 2022-24: Rs2.30L before tax / Rs1.82L business income (worst fall -19%); Rs1L 2025-26: Rs1.24L / Rs1.17L (worst fall -11%); 754+267 trades, 66% won. Found robust: every neighbour setting (dip depth, stop, target) stayed positive in both windows, broad across 7 of 9 coins | paper, own account `CRYPTO_DIP_REVERSAL` (Rs1L; `engine/crypto_dip_reversal.py`, `hft-crypto-dip-reversal.timer` every 5 min; CoinDCX live prices, Binance candles/funding) + New backtest page |
| crypto | ~~`crypto/strategies/tf_1hour/compounder_5lakh/`~~ | 1-hour trend/momentum on BTC/ETH/SOL perpetual futures, BTC EMA200+ADX regime gate, asymmetric 1.5x/3.8x ATR stop/target | REMOVED 2026-10-21, replaced by `dip_reversal` above (git history has it) |
| crypto | ~~`crypto/strategies/tf_4hour/trend_breakout/`~~ and ~~`crypto/strategies/tf_daily/calm_coins/`~~ | 4-hour trend breakout; calm-market long-only rule on 5 coins | REMOVED 2026-10-20, replaced by `compounder_5lakh` then `dip_reversal` (git history has them) |
| crypto | ~~`crypto/strategies/tf_1min/sr_sweep/`~~ | 1-minute S/R sweep-and-reclaim scalper (68% of trades hit a 0.5R target, -0.14% per trade after fees; even at Delta Exchange's free-close costs with real limit fills -0.04%) | REMOVED 2026-10-17 (git history has it) |
| equity | `strategies/tf_daily/trend_ema_cross/` | 20/50 EMA cross above the 200 EMA in a healthy market (breadth >= 50%), 2.5-ATR stop then 3.25-ATR trailing stop (3 until 2026-10-14); 20 best stocks + 5 US/Hong Kong ETFs (bought only within 3% of NAV, sold above 15%), max 3; sells a trade still in loss after 20 trading days | paper, 1/4 of the `EQUITY_SWING` account (`engine/equity_swing.py`, its own `entry()`; moved from `hft-dryrun.service` 2026-10-16), 2% risk |
| equity | `strategies/tf_daily/breakout_pair/` | two swing breakouts on the day's 200 most-traded NSE stocks (no hindsight list), half the money each: nr7 = narrowest range of 7 days above EMA50 > EMA200 -> buy-stop at that high (max 5); d55 = close above the 55-day high above EMA200 -> buy at the open (max 3); breadth >= 50%, 2% risk to a 2.5-ATR stop, 3.25-ATR trail, 20-day dead money | paper, 1/4 of the `EQUITY_SWING` account (Rs4L, `engine/equity_swing.py`, `hft-equity-swing.timer` 09:08-15:31 IST; data `markets/equity/all_daily.py`, nightly) |
| equity | `strategies/tf_daily/earnings_drift/` | buy after quarterly results beat the market by > 4% over 2 days on 2x volume (NSE results dates, `markets/equity/results_dates.py`), at the open 2 days after; hold 40 trading days, no stop; max 5; breadth >= 50% | paper, 1/4 of the `EQUITY_SWING` account |
| equity | `strategies/tf_quarterly/momentum_portfolio/` | hold the 10 stocks with the best 6-month return / volatility among the day's 200 most-traded NSE stocks; quarterly rebalance at the next open, a holding stays while in the top 20; no market switch | paper, 1/4 of the `EQUITY_SWING` account |
| commodity | `strategies/tf_monthly/trend_carry/` | monthly: buy the 3 of 8 MCX commodities with the best rank(12-month trend) + rank(carry = near vs next contract), sized by 1/volatility, mini contracts, whole lots | paper, own account `MCX_TREND_CARRY` (Rs5L, 1.5x exposure; `engine/mcx_trend_carry.py`, `hft-mcx-trend-carry.timer` 09:10 / 23:40 IST); web backtest too |
| currency | `strategies/tf_daily/rsi_range/` | RSI(14) < 30 buy / > 70 short (only in the outer 25% of the 20-day range), exit at the 20-day average or ~15 days, 2.5-ATR stop | paper, 4% risk (RBI rule: research/paper only) |

Tested and rejected (in the docs, `var/reports/` and git history): intraday and scalping ideas in every market
(`docs/CURRENCY_RESEARCH.md`), currency daily rules, MCX commodity swing / trend / intraday research.
