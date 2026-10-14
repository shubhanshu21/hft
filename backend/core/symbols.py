"""Which market a traded symbol belongs to -- the classification every runner needs, kept apart from any one strategy."""
from __future__ import annotations

from markets.equity.universe import NIFTY50_SYMBOLS

CURRENCY_SYMBOLS = {"USDINR", "EURINR", "GBPINR"}          # JPYINR removed 2026-10-14 (user: "remove JPY totally")
EQUITY_SYMBOLS = set(NIFTY50_SYMBOLS)
COMMODITY_SYMBOLS = {"GOLDPETAL", "CRUDEOILM"}          # MCX, back since 2026-10-14 for the gold_crude strategy


def is_currency(sym: str) -> bool:
    return sym.upper() in CURRENCY_SYMBOLS


def is_equity(sym: str) -> bool:
    return sym.upper() in EQUITY_SYMBOLS


def is_commodity(sym: str) -> bool:
    return sym.upper() in COMMODITY_SYMBOLS


def market_of(sym: str) -> str:
    """"equity" | "currency" | "commodity". Any other symbol is an error (MCX was removed 2026-10-12 and re-added for GOLDPETAL / CRUDEOILM 2026-10-14)."""
    if is_equity(sym):
        return "equity"
    if is_currency(sym):
        return "currency"
    if is_commodity(sym):
        return "commodity"
    raise ValueError(f"{sym!r} is not an NSE currency pair {sorted(CURRENCY_SYMBOLS)}, a NIFTY50 stock or a traded MCX contract {sorted(COMMODITY_SYMBOLS)}")
