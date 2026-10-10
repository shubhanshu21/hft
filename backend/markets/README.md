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

## Strategies (2026-10-10; paper trading only -- no real orders anywhere)

Only strategies that made money after costs in testing are kept. The 5-minute scalpers (commodity, USDINR, equity) were deleted on
2026-10-10 after losing once costs and real fills were counted (`git log -- backend/markets/*/strategies/tf_5min/scalping` to see them).

| market | folder | what it does | status | backtest / doc |
|---|---|---|---|---|
| currency | `strategies/tf_5min/parity` | fades the gap between EURINR / GBPINR and EUR/USD (GBP/USD) x USDINR; 5 lots, 30 min, fills walk the live book | **paper from 2026-10-12** | `python3 -m markets.currency.strategies.tf_5min.parity.backtest`; `docs/CURRENCY_RESEARCH.md` |
| commodity | `strategies/tf_daily/swing` | 12-month time-series momentum on GOLDTEN / SILVERMIC / CRUDEOILM, held for weeks | paper (weak edge) | `docs/SWING_RESEARCH.md` |
| crypto | `strategies/tf_1hour/momentum.py`, `router.py` | slow volatility-targeted trend ensemble with a BTC regime router (BTC / ETH / SOL) | paper on Binance's demo exchange (`hft-crypto.service`) | `docs/CRYPTO_MOMENTUM.md`, `docs/CRYPTO_ALL_WEATHER.md` |
| crypto | `strategies/tf_15min/breakout.py` | daily volatility breakout, long-only with a trend filter (+5-9% a year on its 2025-26 test) | paper (`engine/crypto_breakout.py`) | `docs/CRYPTO_ACTIVE_STRATEGIES.md` |

Equity has no strategy now (`markets/equity/` keeps its cost model, data top-up and universe for research).
Crypto strategies are single modules rather than folders: crypto runs on its own engine (`engine/crypto_paper.py`), not the
`core/strategy.py` framework the other markets share.

Tested and rejected (in the docs and `var/reports/`, never added as strategies): currency daily rules (carry, trend, reversal, gap,
month-end, calendar spread), MCX fair-value and mini-vs-full lags, commodity inventory / gold-silver / intraday-momentum ideas, the YouTube
"Z-score + Hull" and "RSI divergence" commodity scalpers and the two-candle crypto breakout scalper -- see `docs/CURRENCY_RESEARCH.md`
and `docs/COMMODITY_INTRADAY_RESEARCH.md`.
