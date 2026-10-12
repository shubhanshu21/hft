"""Which market a traded symbol belongs to -- the classification every runner needs, kept apart from any one strategy."""
from __future__ import annotations

from markets.equity.universe import NIFTY50_SYMBOLS

CURRENCY_SYMBOLS = {"USDINR", "EURINR", "GBPINR", "JPYINR"}
EQUITY_SYMBOLS = set(NIFTY50_SYMBOLS)


def is_currency(sym: str) -> bool:
    return sym.upper() in CURRENCY_SYMBOLS


def is_equity(sym: str) -> bool:
    return sym.upper() in EQUITY_SYMBOLS


def market_of(sym: str) -> str:
    """"equity" | "currency". Any other symbol is an error: there is no third market (MCX commodity was removed 2026-10-12)."""
    if is_equity(sym):
        return "equity"
    if is_currency(sym):
        return "currency"
    raise ValueError(f"{sym!r} is neither an NSE currency pair {sorted(CURRENCY_SYMBOLS)} nor a NIFTY50 stock")
