# markets/ -- one folder per market, one folder per strategy

```
markets/
  <market>/                      commodity | currency | equity | crypto
    costs.py  data.py  features.py   shared by every strategy of that market (cost model, data top-up, indicators)
    strategies/
      <name>/                    one folder per strategy
        strategy.py              the Strategy class (core/strategy.py), exposes STRATEGY -- found automatically
        backtest.py              its backtest, reusing the same entry code
        ...                      anything else it needs (entry_signal.py, signal.py, proxy.py)
    experiments/                 throwaway research scripts, never run in production
```

Add a strategy: `python3 cli.py new-strategy --market currency --name my_idea` creates
`markets/currency/strategies/my_idea/strategy.py` from a template. The engine discovers it by itself (`core/registry.py`); it only
trades once named in `.env`, e.g. `CURRENCY_STRATEGIES=scalping,parity,my_idea` (and optionally `CURRENCY_MY_IDEA_SYMBOLS=...` to
limit it to some symbols). Guide: `docs/ADDING_A_STRATEGY.md`. Project rule: a credible backtest (>= 15 trades) and a held-out test
window before it is switched on, then paper trading.

## Strategies (status 2026-10-10; paper trading only -- no real orders anywhere)

| market | strategy | what it does | status | backtest / doc |
|---|---|---|---|---|
| currency | `strategies/parity` | fades the gap between EURINR / GBPINR and EUR/USD (GBP/USD) x USDINR; 5 lots, 30 min, fills walk the live book | **paper from 2026-10-12** | `python3 -m markets.currency.strategies.parity.backtest`; `docs/CURRENCY_RESEARCH.md` |
| currency | `strategies/scalping` | 5-minute trend-breakout scalper, USDINR only (`CURRENCY_SCALPING_SYMBOLS`) | paper; losing after costs, review 2026-10-19 | `python3 -m markets.currency.strategies.scalping.backtest` |
| commodity | `strategies/swing` | 12-month time-series momentum on GOLDTEN / SILVERMIC / CRUDEOILM, held for weeks | paper (weak edge) | `docs/SWING_RESEARCH.md` |
| commodity | `strategies/scalping` | 5-minute trend-breakout scalper | off: no edge once stops fill at real prices | `python3 cli.py backtest`; `docs/FILL_MODEL_AUDIT.md` |
| equity | `strategies/scalping` | NIFTY50 5-minute scalper, ADX-scaled trailing exit | off (`ENABLE_EQUITY_TRADING=false`) | `python3 -m markets.equity.strategies.scalping.backtest` |
| crypto | `strategies/momentum.py`, `strategies/router.py` | slow volatility-targeted trend ensemble with a BTC regime router (BTC / ETH / SOL) | paper on Binance's demo exchange (`hft-crypto.service`, `engine/crypto_paper.py`) | `docs/CRYPTO_MOMENTUM.md`, `docs/CRYPTO_ALL_WEATHER.md` |
| crypto | `strategies/breakout.py` | breakout rules for `engine/crypto_breakout.py` | research | `docs/CRYPTO_ACTIVE_STRATEGIES.md` |

Crypto strategies are single modules rather than folders: crypto runs on its own engine (`engine/crypto_paper.py`), not the
`core/strategy.py` framework the other three markets share.

Tested and rejected (kept in `experiments/` or the docs, not as strategies): currency daily rules (carry, trend, reversal, gap,
month-end, calendar spread), MCX fair-value and mini-vs-full lags, commodity inventory / gold-silver / intraday-momentum ideas, the
"Z-score + Hull" commodity scalper and the two-candle crypto breakout scalper -- see `docs/CURRENCY_RESEARCH.md` and
`docs/COMMODITY_INTRADAY_RESEARCH.md`.
