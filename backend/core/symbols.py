"""Which market a traded symbol belongs to -- the classification every runner needs, kept apart from any one strategy."""
from __future__ import annotations

from markets.equity.universe import NIFTY50_SYMBOLS

CURRENCY_SYMBOLS = {"USDINR", "EURINR", "GBPINR"}          # JPYINR removed 2026-10-14 (user: "remove JPY totally")
GLOBAL_ETF_SYMBOLS = {"MON100", "MAFANG", "MASPTOP50", "HNGSNGBEES", "MAHKTECH"}   # US / Hong Kong ETFs on NSE, traded by trend_ema_cross (2026-10-14)
ETF_SYMBOLS = set(GLOBAL_ETF_SYMBOLS)                       # niftybees_ml and NIFTYBEES removed 2026-10-14 (no ML version beat buy and hold)
EQUITY_SYMBOLS = set(NIFTY50_SYMBOLS) | ETF_SYMBOLS


def is_currency(sym: str) -> bool:
    return sym.upper() in CURRENCY_SYMBOLS


def is_equity(sym: str) -> bool:
    return sym.upper() in EQUITY_SYMBOLS


def market_of(sym: str) -> str:
    """"equity" | "currency". Any other symbol is an error (MCX commodity and crypto were removed from the project 2026-10-14)."""
    if is_equity(sym):
        return "equity"
    if is_currency(sym):
        return "currency"
    raise ValueError(f"{sym!r} is not an NSE currency pair {sorted(CURRENCY_SYMBOLS)} or a NIFTY50 stock")
