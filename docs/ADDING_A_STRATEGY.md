# Adding a strategy

A strategy is **one class in one file**. You write the decisions (when to enter, how to size, when to exit, what it
costs); the runners do everything else, identically for paper trading (`engine/live_dryrun.py`) and real orders
(`engine/live_trading.py`): kill switches, cooldowns, portfolio-heat and margin limits, sector cap, drawdown-scaled
sizing, order placement and fill handling, DB records, alerts, and restoring the position after a restart.

## The 3 steps

1. **Create** `backend/markets/<market>/<name>/strategy.py` (`<market>` is `commodity`, `currency` or `equity`). The quickest way:

   ```
   cd backend && python3 cli.py new-strategy --market equity --name swing
   ```

   which writes an inert template (its `entry()` never signals) for you to fill in.
2. **Expose** an instance called `STRATEGY` — the registry finds it by itself, there is no list to edit.
3. **Switch it on** in `backend/.env`, then restart the daemon:

   ```
   EQUITY_STRATEGIES=scalping,swing
   ```

   A strategy that is not named there never trades, so dropping a file in is safe. If the variable is absent, only the
   existing scalpers run.

## The class

```python
from core.exits import activation_trail            # ready-made exit managers (or write your own manage())
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy


class EquitySwing(Strategy):
    # ---- declare what kind of strategy this is ----------------------------------------
    name, market = "swing", "equity"
    intraday = False              # held overnight: no end-of-day square-off, no timeout
    product = "D"                 # real orders use Upstox delivery ("I" = intraday)
    uses_leverage = False         # sized and margin-checked at 1x
    allow_short = False           # delivery cannot short; the runner refuses a short signal
    timeframe = ("days", 1)       # the candles entry()/manage() receive
    lookback_days = 250           # how much history to fetch for that timeframe
    max_positions = 5             # cap on this strategy's concurrent positions
    sector_cap = True             # apply the per-sector position cap
    id_prefix = "SW"              # order / position ids

    def due(self, now):           # optional: evaluate once a day, near the close
        return self.in_session(now) and now.hour == 15 and now.minute >= 20

    # ---- decide -----------------------------------------------------------------------
    def entry(self, ctx: EntryContext) -> Signal | None:
        ...                       # look at ctx.candles; return None for "no trade"
        return Signal(
            symbol=ctx.symbol, direction="long", entry_price=px, stop_loss=stop,
            qty=qty, stop_dist=px - stop, instrument_key=ctx.instrument_key,
            exit_state={"target": tgt},           # whatever manage() needs later; saved to the DB
            target_price=tgt, breakeven_price=tgt, alert_levels={"tp": tgt},
            price_levels=("target",),             # exit_state keys that are prices (live re-anchors them to the real fill)
        )

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        ...                       # called every scan while open; return ExitDecision(price, reason) to close
        # you may move the stop: pos["current_stop"] = ...   (the runner persists it)

    def costs(self, symbol, direction, entry, exit_price, qty) -> dict:
        ...                       # delivery taxes differ from intraday: return the same dict shape as
                                  # markets/equity/costs.compute_nse_equity_costs

STRATEGY = EquitySwing()
```

`ctx` gives you the candles, the time, `capital` (the whole account, or less once other positions hold margin),
`risk_pct` (already scaled down by the drawdown rule), `leverage` (1.0 when `uses_leverage = False`),
`direction_filter` and the regime flags.
Size the position from `ctx.capital` and `ctx.risk_pct` — see `size_equity_shares` / `size_commodity_lots` for the
two existing sizing rules. Reusable exit managers live in `core/exits.py`; the three existing strategies
(`markets/*/scalping/strategy.py`) are complete worked examples.

**Writing your own `manage()`?** Do not read the current bar's raw high/low to decide a stop or target. That bar may contain prices from
*before* your entry (a breakout signal fires on a wide bar, so the bar's low is often already past the stop) and the position would be
stopped out on the very next scan by a price it never traded at. Use `core.exits.post_entry_range(pos, bar)` — the runner records
`pos["entry_bar_ts"]` for you — or call the ready-made managers in `core/exits.py`, which already do this.

## What you get for free (and cannot bypass)

| Handled by the runner | Detail |
|---|---|
| Kill switches, per-market cooldown | a paused market or a tripped symbol never reaches `entry()` |
| Heat and margin limits | `core/risk.py`; a new entry is **sized to the margin still available** |
| One position per symbol | across all strategies — a second strategy is skipped while a symbol is held |
| Persistence | strategy name + `exit_state` are stored, so an overnight position is managed correctly after a restart |
| Real orders | product type, funds backstop at the strategy's leverage, fill confirmation, stop/target re-anchored to the real fill |
| Bookkeeping | orders, trades (tagged with the strategy), snapshots, Telegram alerts |

## Before it trades

This project's rule applies: a strategy is not switched on until it has a credible backtest (≥15 trades) **and** a
genuine held-out test window. Put the backtest next to it (`markets/<market>/<name>/backtest.py`), reuse the same
`entry()` logic so backtest and live cannot drift, and paper-trade it before `ALLOW_LIVE_TRADING` is even considered.

## Tests

`tests/test_strategy_framework.py` contains a small overnight strategy driven through both runners — copy it as a
template for what to check. `tests/test_strategy_parity.py` locks the existing strategies' behaviour; run it with
`RUN_REPLAY=1` for the full bar-by-bar replay.
