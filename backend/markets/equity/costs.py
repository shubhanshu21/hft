"""
markets/equity/costs.py -- NSE Equity Intraday (MIS) statutory cost model.

Rebuilt 2026-09-19 (see markets/equity/universe.py's docstring for the
rebuild context). Rates below are the same ones the prior removed equity
scalper's README table documented, cross-checked against this project's
CTT/currency cost modules' own documented rate-verification discipline
(see commodity_costs.py's RATES_LAST_VERIFIED) -- re-verify against a
current public rate source if this goes stale.
"""
from __future__ import annotations

from core.slippage import adaptive_slippage_per_leg

RATES_LAST_VERIFIED = "2026-09-19"

EQUITY_STT_PCT_SELL_SIDE   = 0.025   # 0.025% on sell-side turnover (intraday equity delivery STT differs -- this is intraday MIS)
EQUITY_EXCHANGE_TXN_PCT    = 0.00325 # NSE turnover fee on total turnover
EQUITY_SEBI_PCT            = 0.0001  # Rs10 per crore
EQUITY_STAMP_DUTY_PCT      = 0.003   # 0.003% on buy-side turnover
GST_RATE                   = 0.18
# Upstox's own brokerage calculator (ChargeApi.get_brokerage), checked 2026-09-24 for equity MIS, MCX and NCD: min(0.06% of turnover, Rs30) per order.
# The model here used min(0.05%, Rs20) and understated every round trip by ~Rs23.6 (+GST) -- see tests/test_costs_vs_upstox.py.
UPSTOX_BROKERAGE_CAP       = 30.0    # flat Rs30 cap per executed order leg
UPSTOX_BROKERAGE_PCT       = 0.06    # 0.06% turnover, whichever is lower


def compute_equity_brokerage(trade_val: float) -> float:
    base = min(trade_val * (UPSTOX_BROKERAGE_PCT / 100), UPSTOX_BROKERAGE_CAP)
    return base * (1 + GST_RATE)


def compute_nse_equity_costs(direction: str, entry: float, exit_p: float, qty: int, tick_size: float = 0.05, symbol: str = "") -> dict:
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

    # Equity slippage: empirical median half-spread when available; fall back to ½ tick.
    slip_per_leg = adaptive_slippage_per_leg(symbol, tick_size * 0.5) if symbol else tick_size * 0.5
    slip = slip_per_leg * qty * 2  # entry leg + exit leg

    total_friction = brok + stt + stamp + exch + sebi + gst_charges + slip
    net = gross - total_friction

    return {
        "gross": round(gross, 2), "brokerage": round(brok, 2), "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2), "exchange_txn": round(exch, 2), "sebi": round(sebi, 2),
        "gst": round(gst_charges, 2), "slippage": round(slip, 2),
        "total": round(total_friction, 2), "net": round(net, 2), "qty": qty,
    }


# ---- Delivery (CNC) -- for overnight / swing strategies ------------------------------------------------
# Checked against Upstox's published charges on 2026-09-23 (upstox.com/calculator/brokerage-calculator): delivery
# STT 0.1% on BOTH buy and sell, stamp duty 0.015% on the buy side, DP charge Rs18.50 + 18% GST per scrip on each sell,
# brokerage the lower of 0.1% or Rs20 per order (some aggregators list delivery brokerage as Rs0; Rs20 is the
# conservative reading). Exchange / SEBI fees are the same constants as above. Intraday rates above are unchanged.
DELIVERY_RATES_LAST_VERIFIED = "2026-09-23"
DELIVERY_STT_PCT_BOTH_SIDES = 0.1
DELIVERY_STAMP_DUTY_PCT_BUY = 0.015
DELIVERY_DP_CHARGE = 18.50            # per scrip per sell day, before GST
DELIVERY_BROKERAGE_PCT = 0.1
DELIVERY_BROKERAGE_CAP = 30.0             # Upstox calculator 2026-09-24: Rs30 on a Rs1.2 lakh delivery order


def compute_nse_equity_delivery_costs(direction: str, entry: float, exit_p: float, qty: int, tick_size: float = 0.05, symbol: str = "") -> dict:
    """Itemised costs of a delivery (held overnight) equity round trip. Same dict shape as compute_nse_equity_costs.
    Long only: delivery cannot short."""
    if direction.lower() != "long":
        raise ValueError("equity delivery is long-only")
    ev, xv = qty * entry, qty * exit_p
    gross = qty * (exit_p - entry)

    def brok_leg(v: float) -> float:
        return min(v * DELIVERY_BROKERAGE_PCT / 100, DELIVERY_BROKERAGE_CAP)
    brok = (brok_leg(ev) + brok_leg(xv)) * (1 + GST_RATE)
    stt = (ev + xv) * DELIVERY_STT_PCT_BOTH_SIDES / 100
    stamp = ev * DELIVERY_STAMP_DUTY_PCT_BUY / 100
    exch = (ev + xv) * (EQUITY_EXCHANGE_TXN_PCT / 100)
    sebi = (ev + xv) * (EQUITY_SEBI_PCT / 100)
    dp = DELIVERY_DP_CHARGE * (1 + GST_RATE)
    gst_charges = (exch + sebi) * GST_RATE
    slip_per_leg = adaptive_slippage_per_leg(symbol, tick_size * 0.5) if symbol else tick_size * 0.5
    slip = slip_per_leg * qty * 2
    total_friction = brok + stt + stamp + exch + sebi + dp + gst_charges + slip
    return {
        "gross": round(gross, 2), "brokerage": round(brok + dp, 2), "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2), "exchange_txn": round(exch, 2), "sebi": round(sebi, 2),
        "gst": round(gst_charges, 2), "slippage": round(slip, 2),
        "total": round(total_friction, 2), "net": round(gross - total_friction, 2), "qty": qty,
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
    shares_margin = math.floor(capital / margin_per_share) if margin_per_share > 0 else 1     # 0 when one share does not fit the account
    return min(shares_risk, shares_margin)
