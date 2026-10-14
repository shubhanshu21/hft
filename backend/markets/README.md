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

## Strategies (2026-10-14; paper trading only -- no real orders anywhere)

Crypto (the Binance paper trader and its strategies) and the MCX `gold_crude` strategy were removed on 2026-10-14 (user: "remove commodity and
crypto folders"; git history has them). MCX DATA IS STILL DOWNLOADED (user: "keep mcx nightly jobs ... for downloading data"):
`commodity/data.py` (nightly "mcx data") and `commodity/daily_series.py` (nightly "commodity daily series"); the order book is recorded by
`engine/depth_recorder.py`. Earlier deletions: the 5-minute scalpers (2026-10-10), the EURINR/GBPINR parity scalp and the crypto daily breakout (2026-10-12).

| market | folder | what it does | status |
|---|---|---|---|
| equity | `strategies/tf_daily/trend_ema_cross/` | 20/50 EMA cross above the 200 EMA in a healthy market (breadth >= 50%), 2.5-ATR stop then 3.25-ATR trailing stop (3 until 2026-10-14); 20 best stocks, max 3 | paper (`hft-dryrun.service`), 2% risk |
| equity | `strategies/tf_daily/niftybees_ml/` | NIFTYBEES (Nifty 50 ETF): hold unless a weekly ML forecast (random-features ridge, 25 Nifty/breadth/FII/VIX features, 20-day horizon, monthly walk-forward retraining) turns negative | backtest page only (sharing the paper account with trend_ema_cross cut 2023-26 from Rs2.50L to Rs2.17L) |
| currency | `strategies/tf_daily/rsi_range/` | RSI(14) < 30 buy / > 70 short, exit at the 20-day average or ~15 days, 2.5-ATR stop | paper, 4% risk (RBI rule: research/paper only) |

Tested and rejected (in the docs, `var/reports/` and git history): intraday and scalping ideas in every market
(`docs/CURRENCY_RESEARCH.md`), currency daily rules, MCX commodity swing / trend / intraday research.
