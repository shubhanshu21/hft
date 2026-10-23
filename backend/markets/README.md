# markets/ -- market -> timeframe -> strategy

NSE equity and currency were removed entirely 2026-10-21 (user: "remove equity and currency because it does not giving any profits"):
every strategy under `markets/equity/strategies/` and `markets/currency/strategies/`, their paper accounts (EQUITY_SWING,
DRYRUN_ACCOUNT), `engine/equity_swing.py`, `hft-dryrun.service`, `hft-equity-swing.timer`, and the nightly jobs that only fed them
(equity daily/ETF/all-NSE/results-dates top-ups, currency data/daily-series top-ups) are gone -- see git history. Two small files
were kept because unrelated research code still needs them (not live strategies): `markets/equity/universe.py` (the NIFTY50 symbol
list, used by `engine/research_data.py`'s equity-1m/delivery/events jobs) and `markets/currency/costs.py` (lot-size constants, used
by `engine/live_dryrun.py`/`engine/live_trading.py`'s generic cost-calculation code, which is otherwise dead with nothing left to
trade in those markets). What paper trading runs now: commodity (`trend_carry`) and crypto (`dip_reversal`).

```
markets/
  <market>/                          commodity | crypto
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

Add a strategy: `python3 cli.py new-strategy --market <market> --timeframe 15min --name my_idea` creates
`markets/<market>/strategies/tf_15min/my_idea/strategy.py` from a template (timeframes: `5min`, `15min`, `1hour`, `daily`). The engine
discovers it by itself (`core/registry.py`, `MARKETS = ("currency", "equity")` -- unused now that both are removed, but still the
mechanism a future daily-bar market strategy would use; commodity/crypto instead wire in their own `web_backtest()`/`backtest()`
function directly, see `dip_reversal`/`trend_carry`). Names must be unique within a market, whatever the timeframe.
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
| crypto | `crypto/strategies/tf_5min/dip_reversal/` | 5-minute dip-buy reversal on 11 coins' perpetual futures (BTC ETH SOL BNB XRP DOGE ADA AVAX DOT LTC TRX -- LINK removed 2026-10-22, lost money in BOTH dev (Rs-2.66L) and the held-out test window (Rs-0.29L), a genuine non-edge found before removal, not a hindsight pick; removing it improved both windows, dev Rs17.45L->Rs19.79L, test Rs2.44L->Rs2.92L, worst fall -76%/-47% -> -75%/-45%): a Bollinger-band squeeze close (BinHV45) or a slip under the 20-candle lower band below the 50-candle EMA (ClucMay2018) -> long; exit at a 15% target (limit), a 7% stop, the close back above the 20-candle mid-band, or a 24h time stop (our own safety cap). **2026-10-22: crowd-shrink rule (CROWD_K) built and tested, then left OFF -- user's final choice is RISK_PCT=8%, no crowd rule.** Options tested: when n coins signal on the same 5-min bar and n>4, each new trade is sized 4/n. Business tax, Rs1L: from 2022 old Rs65.2L / K4-12% Rs104.5L / K3-13% Rs83.7L; from 2023 Rs34.6L / Rs37.3L / Rs27.5L; worst fall -75% / -72% / -64%; 2026-08-22 -11.9% / -19.2% / -11.5% -- K=4 does not shrink a 4-coin crash like that day, K=3 does but costs profit in calm years; every one of the 8 worst days 2022-26 incl. 2026-08-22 was a market-wide crash with 5-10 coins signalling at once). Old 8%/no-rule vs new, Rs1L, real costs: dev 2022-24 Rs43.4L -> Rs56.4L, test 2025-26 Rs4.6L -> Rs6.3L, worst drop -75%/-46% -> -72%/-37%, worst day -55% -> -51%; every k 3-5 x risk 10-14% beat the old setting on profit per unit of drawdown (not one lucky cell). Rejected on the way: pause after stop-outs (StoplossGuard), BTC-drop filters, total-exposure caps, skipping crowded signals, portfolio circuit breaker. The text below about 8% is the earlier history. RISK_PCT base (dynamically scaled 0.6x-1.8x by the market's own 20-day realized volatility vs its long-run median -- size up when calm, down when choppy, same idea as the old calm_coins volatility gate), up to 5 positions open together (shared margin pool); taker fee 0.05%+18% GST+slippage on entries/stops, maker fee 0.02%+GST on the target, real funding, gapped stops at the open. **8% risk was the user's explicit, informed choice for bigger profit over 2%'s safety** (2026-10-21, "see take a risk man and i need bigger profits") -- 2% was the risk-adjusted-best setting (worst fall -19%, see RISK_PCT history above in this file's git log); 10% is past the real ceiling (some years make LESS at 10% than at 8% because the drawdown eats the recovery); 8% is the highest risk level that still grows monotonically with more risk. One continuously-compounding account (not reset each year -- the real way the paper account runs): worst drawdown seen was -76% at points along the path (the account could fall to roughly a quarter of its peak value before recovering); every calendar year 2022-2026 was individually profitable at every risk level tested 2%-10%. The 15%/7%/5 target/stop/concurrency rule (2026-10-21 joint sweep, beat the original symmetric 7%/7%/3 in both dev and test, a whole region of the grid, not one lucky cell) still applies on top of the 8% risk; trailing stops and a numeric confirmation filter were both tried 2026-10-22 and made things worse in both windows, rejected. Shorts, pairs trading and a funding-rate filter were also tried and rejected. Real order placement on Delta India's demo account (`services/broker/delta_broker.py`, resting limit targets, margin-aware sizing) exists and is tested, but is OFF by default since 2026-10-22 (`DELTA_PLACE_REAL_ORDERS=false` in `.env`) -- the demo account's small margin routinely clamped real orders below the intended size, which is less representative than full simulation; Delta's live price feed is still used either way | paper, own account `CRYPTO_DIP_REVERSAL` (Rs1L; `engine/crypto_dip_reversal.py`, `hft-crypto-dip-reversal.timer` every 5 min; Delta Exchange India live prices, Binance candles/funding) + New backtest page |
| crypto | ~~`crypto/strategies/tf_1hour/compounder_5lakh/`~~ | 1-hour trend/momentum on BTC/ETH/SOL perpetual futures, BTC EMA200+ADX regime gate, asymmetric 1.5x/3.8x ATR stop/target | REMOVED 2026-10-21, replaced by `dip_reversal` above (git history has it) |
| crypto | ~~`crypto/strategies/tf_4hour/trend_breakout/`~~ and ~~`crypto/strategies/tf_daily/calm_coins/`~~ | 4-hour trend breakout; calm-market long-only rule on 5 coins | REMOVED 2026-10-20, replaced by `compounder_5lakh` then `dip_reversal` (git history has them) |
| crypto | ~~`crypto/strategies/tf_1min/sr_sweep/`~~ | 1-minute S/R sweep-and-reclaim scalper (68% of trades hit a 0.5R target, -0.14% per trade after fees; even at Delta Exchange's free-close costs with real limit fills -0.04%) | REMOVED 2026-10-17 (git history has it) |
| commodity | `strategies/tf_monthly/trend_carry/` | monthly: buy the 3 of 8 MCX commodities with the best rank(12-month trend) + rank(carry = near vs next contract) (ties to the stronger trend, fixed 2026-10-23 -- ties had gone to list order, which inflated earlier numbers; skewness and basis-momentum were tested as extra ranks and rejected on a fair re-test: trend+carry Rs2.5L 2023-26 +159% vs +skew +147%), nearest-lot rounding (2026-10-23), sized by 1/volatility, mini contracts, whole lots | paper, own account `MCX_TREND_CARRY` (**Rs2.5L since 2026-10-21**, 1.5x exposure; `engine/mcx_trend_carry.py`, `hft-mcx-trend-carry.timer` 09:10 / 23:40 IST); web backtest too. Capital check 2026-10-21 (user: "do we really need 5lakhs"): below ~Rs2-2.5L whole-lot minimums starve it of trades (Rs1L 2022-26 only made +9%); above ~Rs2.5L the % return barely changes with more capital (Rs2.5L-Rs8L all land near +145-180%) -- Rs5L wasn't a real requirement, just a round number. Reset to Rs2.5L (DB backed up first) |

Tested and rejected (in the docs, `var/reports/` and git history): intraday and scalping ideas in every market
(`docs/CURRENCY_RESEARCH.md`), currency daily rules, MCX commodity swing / trend / intraday research.
