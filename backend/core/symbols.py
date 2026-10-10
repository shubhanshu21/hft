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
    """"equity" | "currency" | "commodity"."""
    return "equity" if is_equity(sym) else "currency" if is_currency(sym) else "commodity"
