"""Long daily history for MCX symbols, used to compute swing signals.

The current MCX contract has only 1-4 months of daily history (monthly expiry), too short for a 252-day signal. The
rules were therefore researched -- and the live strategy computes its signal -- on the matching global futures priced
in rupees (USD futures x USDINR). It is a PROXY: continuous futures are not roll-adjusted, and the MCX contract can
differ from it by the import-parity premium. Execution and stops use the real MCX price; only the DIRECTION and the
ATR-based stop distance (as a % of price) come from here.
"""
from __future__ import annotations

import pandas as pd

from services.data.yahoo import fetch_daily

# MCX symbol -> Yahoo global futures ticker
MCX_PROXY = {"CRUDEOILM": "CL=F", "GOLDM": "GC=F", "SILVER": "SI=F", "NATGASMINI": "NG=F", "COPPER": "HG=F"}


def inr_frame(symbol: str) -> pd.DataFrame:
    """Daily OHLC of the global futures for an MCX symbol, in rupees. Index = date."""
    usd = fetch_daily(MCX_PROXY[symbol])
    rate = fetch_daily("INR=X")["close"].reindex(usd.index).ffill().bfill()
    return pd.DataFrame({k: usd[k] * rate for k in ("open", "high", "low", "close")}).dropna()
