"""Premium of the international ETFs: how far the NSE closing price is above the fund's real value (NAV). Run nightly by engine/nightly.py.

    python3 -m markets.equity.etf_premium

RBI / SEBI cap Indian funds' overseas investment; when a fund house hits the cap it stops creating units and the ETF can trade far above
its NAV (MAFANG +58% on 2026-10-13). trend_ema_cross buys these ETFs only below PREMIUM_BUY_MAX and sells above PREMIUM_SELL_MAX.
NAV: AMFI's daily NAV history via api.mfapi.in (official AMFI data); price: the nightly daily candles (var/archive/equity/<SYM>_daily.csv).
Writes var/archive/etf/<SYM>_premium.csv (date, premium_pct).
"""
from __future__ import annotations

import sys

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT

AMFI_CODES = {"MON100": 114984, "MAFANG": 148927, "MASPTOP50": 149169, "HNGSNGBEES": 140095, "MAHKTECH": 149379}


def nav_history(code: int) -> pd.Series:
    j = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=60).json()
    return pd.Series({pd.to_datetime(x["date"], format="%d-%m-%Y"): float(x["nav"]) for x in j["data"]}).sort_index()


def premium_path(symbol: str):
    return ARCHIVE_ROOT / "etf" / f"{symbol}_premium.csv"


def update(symbol: str) -> pd.Series:
    nav = nav_history(AMFI_CODES[symbol])
    px = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{symbol}_daily.csv")
    px = px.set_index(pd.to_datetime(px["timestamp"].str[:10]))["close"]
    prem = ((px / nav.reindex(px.index).ffill() - 1) * 100).dropna()
    premium_path(symbol).parent.mkdir(parents=True, exist_ok=True)
    prem.rename("premium_pct").rename_axis("date").to_csv(premium_path(symbol))
    return prem


def main() -> int:
    failed = []
    for s in AMFI_CODES:
        try:
            p = update(s)
            print(f"{s}: premium {p.iloc[-1]:+.1f}% on {p.index[-1].date()}")
        except Exception as e:                                   # one fund failing must not stop the others
            failed.append(s)
            print(f"{s}: FAILED {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
