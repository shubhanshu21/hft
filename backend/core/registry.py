"""Strategy discovery and selection.

Every markets/<market>/<name>/strategy.py that exposes STRATEGY (a Strategy instance) or
STRATEGIES (a list of them) is found automatically -- there is no list to edit. Which ones
actually RUN is chosen per market in .env:

    COMMODITY_STRATEGIES=scalping
    CURRENCY_STRATEGIES=scalping
    EQUITY_STRATEGIES=scalping,swing

If the variable is absent, only strategies with default_enabled = True run (the existing
scalpers), so dropping in a new strategy file never starts trading by itself.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
from functools import lru_cache

import markets
from core.strategy import Strategy

MARKETS = ("commodity", "currency", "equity")


@lru_cache(maxsize=1)
def discover() -> dict[tuple[str, str], Strategy]:
    """{(market, name): strategy} for every strategy file under markets/."""
    found: dict[tuple[str, str], Strategy] = {}
    for mod in pkgutil.walk_packages(markets.__path__, markets.__name__ + "."):
        if not mod.name.endswith(".strategy"):
            continue
        module = importlib.import_module(mod.name)      # a broken strategy file fails loudly, naming itself
        items = list(getattr(module, "STRATEGIES", []))
        if getattr(module, "STRATEGY", None) is not None:
            items.append(module.STRATEGY)
        for s in items:
            if not isinstance(s, Strategy) or not s.name or s.market not in MARKETS:
                raise TypeError(f"{mod.name}: {s!r} must be a Strategy with a name and market in {MARKETS}")
            key = (s.market, s.name)
            if key in found:
                raise ValueError(f"Duplicate strategy {key}: {mod.name} and an earlier module both define it")
            found[key] = s
    return found


def get(market: str, name: str) -> Strategy:
    """Look up any discovered strategy, enabled or not (an open position must still be managed
    by its own strategy even if it has since been switched off in .env)."""
    try:
        return discover()[(market, name)]
    except KeyError:
        raise KeyError(f"No strategy '{name}' for market '{market}'. Known: "
                       f"{sorted(n for m, n in discover() if m == market)}") from None


def active(market: str) -> list[Strategy]:
    """Strategies switched on for `market`, in the order they are listed in <MARKET>_STRATEGIES."""
    raw = os.environ.get(f"{market.upper()}_STRATEGIES")
    known = {n: s for (m, n), s in discover().items() if m == market}
    if raw is None or not raw.strip():
        return [s for s in known.values() if s.default_enabled]
    chosen = []
    for name in (n.strip() for n in raw.split(",") if n.strip()):
        if name not in known:
            raise KeyError(f"{market.upper()}_STRATEGIES names unknown strategy '{name}'. Known: {sorted(known)}")
        chosen.append(known[name])
    return chosen


def market_of(symbol: str) -> str:
    """"equity" | "currency" | "commodity" for a traded symbol."""
    from markets.commodity.scalping.entry_signal import is_currency
    from markets.equity.scalping.entry_signal import is_equity
    return "equity" if is_equity(symbol) else "currency" if is_currency(symbol) else "commodity"


def default_strategy(market: str) -> Strategy:
    """The strategy assumed for legacy positions / rows saved before strategies were tagged."""
    return get(market, "scalping")
