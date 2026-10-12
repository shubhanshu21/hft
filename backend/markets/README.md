# markets/ -- market -> timeframe -> strategy

```
markets/
  <market>/                          commodity | currency | equity | crypto
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

Add a strategy: `python3 cli.py new-strategy --market commodity --timeframe 15min --name my_idea` creates
`markets/commodity/strategies/tf_15min/my_idea/strategy.py` from a template (timeframes: `5min`, `15min`, `1hour`, `daily`). The engine
discovers it by itself (`core/registry.py`); it only trades once named in `.env`, e.g. `COMMODITY_STRATEGIES=swing,my_idea` (and
optionally `COMMODITY_MY_IDEA_SYMBOLS=...` to limit it to some symbols). Names must be unique within a market, whatever the timeframe.
A test (`tests/test_strategy_framework.py`) fails if a strategy sits in a folder that does not match its timeframe (`timeframe`, or
`signal_timeframe` when the signal uses other bars than the ones the runner fetches). Guide: `docs/ADDING_A_STRATEGY.md`. Project
rule: a credible backtest (>= 15 trades) and a held-out test window before it is switched on, then paper trading.

## Strategies (2026-10-12; paper trading only -- no real orders anywhere)

Swing only (user, 2026-10-12: "remove scalping, focus on swing strategies, weekly horizon, backtest them first"). Deleted, in git
history: the 5-minute scalpers (2026-10-10), the EURINR/GBPINR parity scalp and the crypto daily breakout (2026-10-12).

| market | folder | what it does | status | backtest / doc |
|---|---|---|---|---|
| commodity | `strategies/tf_daily/swing` | 12-month time-series momentum on GOLDTEN / SILVERMIC / CRUDEOILM, ATR stop, held for weeks | paper; test 2019-26 profit factor 1.88 on roll-honest ETF prices (weak, mostly gold) | `docs/SWING_RESEARCH.md` |
| commodity | `strategies/tf_daily/trend` | 50/200-day EMA trend on the roll-honest ETF proxies (GLD/SLV/USO x INR), ATR stop, held for weeks; trades a contract only when `swing` is not holding it | paper from 2026-10-13; test 2019-26 +4.67% a trade, PF 1.81, 6/8 years (t 1.4, weak) | `docs/SWING_RESEARCH.md` |
| crypto | `strategies/tf_1hour/momentum.py`, `router.py` | slow volatility-targeted trend ensemble with a BTC regime router (BTC / ETH / SOL), holds weeks | paper on Binance's demo exchange (`hft-crypto.service`) | `docs/CRYPTO_MOMENTUM.md`, `docs/CRYPTO_ALL_WEATHER.md` |

Currency and equity have no strategy now (their cost models, data top-ups and research data stay).
Crypto strategies are single modules rather than folders: crypto runs on its own engine (`engine/crypto_paper.py`), not the
`core/strategy.py` framework the other markets share.

Tested and rejected (in the docs and `var/reports/`): intraday and scalping ideas in every market (`docs/CURRENCY_RESEARCH.md`,
`docs/COMMODITY_INTRADAY_RESEARCH.md`), currency daily rules, commodity trend portfolios that did no better than holding gold.
