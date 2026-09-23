"""Generate a new strategy folder from a template: `python3 cli.py new-strategy --market equity --name swing`.

The generated strategy is inert -- entry() never signals -- so it can be committed and discovered without ever
trading. Fill in entry() / manage() / costs(), then name it in <MARKET>_STRATEGIES to switch it on.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.paths import BACKEND_ROOT

MARKETS = ("commodity", "currency", "equity")

_TEMPLATE = '''"""{market} / {name} -- TODO: one line on what this strategy does and why it should have an edge.

Not switched on until it is named in .env:   {MARKET}_STRATEGIES=scalping,{name}
Guide: docs/ADDING_A_STRATEGY.md.  Worked examples: markets/*/scalping/strategy.py
"""
from __future__ import annotations

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy


class {Class}(Strategy):
    # ---- what kind of strategy is this? -------------------------------------------------
    name = "{name}"
    market = "{market}"
    intraday = True               # False = held overnight (no end-of-day square-off, no timeout)
    product = "I"                 # real orders: "I" intraday, "D" delivery
    uses_leverage = True          # False = sized and margin-checked at 1x (delivery)
    allow_short = True            # False = the runner refuses a short signal
    timeframe = ("minutes", 5)    # the candles entry()/manage() receive, e.g. ("days", 1)
    lookback_days = 0             # >0 = fetch that many days of history (needed for daily bars)
    max_positions = None          # cap on this strategy's concurrent positions
    sector_cap = False            # True = apply the per-sector position cap (equity)
    id_prefix = "{PREFIX}"        # order / position ids

    # def due(self, now):         # optional: only evaluate at certain times, e.g. once a day near the close
    #     return self.in_session(now)

    # ---- decide ---------------------------------------------------------------------------
    def entry(self, ctx: EntryContext) -> Signal | None:
        """Return a Signal to enter, or None. ctx.candles is chronological OHLCV; size the position from
        ctx.capital and ctx.risk_pct (see size_equity_shares / size_commodity_lots)."""
        return None               # TODO -- until this returns a Signal the strategy never trades

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        """Called every scan while a position is open. Return ExitDecision(price, reason) to close it; you may
        move the stop with pos["current_stop"] = ... . core/exits.py has ready-made managers."""
        return None               # TODO

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        """Round-trip costs, same dict shape as the market's cost function. Delivery differs from intraday."""
        raise NotImplementedError("TODO: return this strategy's cost model")


STRATEGY = {Class}()
'''


def create(market: str, name: str, root: Path | None = None) -> Path:
    if market not in MARKETS:
        raise ValueError(f"market must be one of {MARKETS}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("name must be lowercase letters, digits and underscores, starting with a letter")
    folder = (root or BACKEND_ROOT / "markets") / market / name
    target = folder / "strategy.py"
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").touch(exist_ok=True)
    target.write_text(_TEMPLATE.format(
        market=market, name=name, MARKET=market.upper(), Class="".join(p.capitalize() for p in name.split("_")) + market.capitalize(),
        PREFIX=name[:3].upper(),
    ))
    return target
