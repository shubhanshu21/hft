"""
strategy/equity_costs.py -- NSE Equity Intraday (MIS) statutory cost model.

Rebuilt 2026-09-19 (see strategy/equity_universe.py's docstring for the
rebuild context). Rates below are the same ones the prior removed equity
scalper's README table documented, cross-checked against this project's
CTT/currency cost modules' own documented rate-verification discipline
(see commodity_costs.py's RATES_LAST_VERIFIED) -- re-verify against a
current public rate source if this goes stale.
"""
from __future__ import annotations

RATES_LAST_VERIFIED = "2026-09-19"

EQUITY_STT_PCT_SELL_SIDE   = 0.025   # 0.025% on sell-side turnover (intraday equity delivery STT differs -- this is intraday MIS)
EQUITY_EXCHANGE_TXN_PCT    = 0.00325 # NSE turnover fee on total turnover
EQUITY_SEBI_PCT            = 0.0001  # Rs10 per crore
EQUITY_STAMP_DUTY_PCT      = 0.003   # 0.003% on buy-side turnover
GST_RATE                   = 0.18
UPSTOX_BROKERAGE_CAP       = 20.0    # flat Rs20 cap per executed order leg
UPSTOX_BROKERAGE_PCT       = 0.05    # 0.05% turnover, whichever is lower


def compute_equity_brokerage(trade_val: float) -> float:
    base = min(trade_val * (UPSTOX_BROKERAGE_PCT / 100), UPSTOX_BROKERAGE_CAP)
    return base * (1 + GST_RATE)


def compute_nse_equity_costs(direction: str, entry: float, exit_p: float, qty: int, tick_size: float = 0.05) -> dict:
    """Computes exact itemized costs for an NSE equity intraday (MIS) trade.
    `qty`: number of shares (equity has no lot-size multiplier -- 1 share = 1 unit)."""
    d = 1 if direction.lower() == "long" else -1
    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = compute_equity_brokerage(ev) + compute_equity_brokerage(xv)
    stt = (xv if d == 1 else ev) * (EQUITY_STT_PCT_SELL_SIDE / 100)
    stamp = (ev if d == 1 else xv) * (EQUITY_STAMP_DUTY_PCT / 100)
    exch = (ev + xv) * (EQUITY_EXCHANGE_TXN_PCT / 100)
    sebi = (ev + xv) * (EQUITY_SEBI_PCT / 100)
    gst_charges = (exch + sebi) * GST_RATE

    # Half-tick-per-leg slippage estimate, same convention as commodity_costs.py
    slip = (0.5 * tick_size * qty) * 2

    total_friction = brok + stt + stamp + exch + sebi + gst_charges + slip
    net = gross - total_friction

    return {
        "gross": round(gross, 2), "brokerage": round(brok, 2), "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2), "exchange_txn": round(exch, 2), "sebi": round(sebi, 2),
        "gst": round(gst_charges, 2), "slippage": round(slip, 2),
        "total": round(total_friction, 2), "net": round(net, 2), "qty": qty,
    }


def size_equity_shares(capital: float, entry_price: float, stop_distance: float, risk_pct: float, leverage: float = 5.0) -> int:
    """Sizes integer share quantity via the same dual risk/margin-cap logic as
    size_commodity_lots/size_currency_lots -- no lot-size multiplier here,
    just a 1-share unit."""
    import math
    if capital <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0
    risk_rupees = capital * (risk_pct / 100.0)
    shares_risk = max(1, math.floor(risk_rupees / stop_distance))
    margin_per_share = entry_price / max(leverage, 1.0)
    shares_margin = max(1, math.floor(capital / margin_per_share)) if margin_per_share > 0 else 1
    return max(1, min(shares_risk, shares_margin))
