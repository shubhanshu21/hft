# markets/ -- market -> timeframe -> strategy

```
markets/
  <market>/                          currency | equity | crypto   (commodity/: MCX data top-up only, no strategies)
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

## Strategies (2026-10-12; paper trading only -- no real orders anywhere)

MCX commodity was removed on 2026-10-12 (user: "remove commodity section totally"): the `swing` (12-month momentum) and `trend`
(50/200-day EMA) strategies, the commodity cost model, features, research scripts and engine support are deleted (git history). Their
last test on the real May-Oct 2026 MCX prices lost Rs24,334 on Rs1 lakh. `commodity/data.py` remains ONLY to keep recording MCX candles
(nightly top-up); the order book is recorded by `engine/depth_recorder.py`. Earlier deletions, also in git history: the 5-minute scalpers
(2026-10-10), the EURINR/GBPINR parity scalp and the crypto daily breakout (2026-10-12).

| market | folder | what it does | status | backtest / doc |
|---|---|---|---|---|
| crypto | `strategies/tf_1hour/momentum.py`, `router.py` | slow volatility-targeted trend ensemble with a BTC regime router (BTC / ETH / SOL), holds weeks | paper on Binance's demo exchange (`hft-crypto.service`) | `docs/CRYPTO_MOMENTUM.md`, `docs/CRYPTO_ALL_WEATHER.md` |

Currency and equity have no strategy now (their cost models, data top-ups and research data stay), so the paper daemon
(`hft-dryrun.service`) is disabled. Crypto strategies are single modules rather than folders: crypto runs on its own engine
(`engine/crypto_paper.py`), not the `core/strategy.py` framework the other markets share.

Tested and rejected (in the docs, `var/reports/` and git history): intraday and scalping ideas in every market
(`docs/CURRENCY_RESEARCH.md`), currency daily rules, MCX commodity swing / trend / intraday research.
