"""MCX futures costs (Upstox Plus) and lot units for the contracts commodity/trend_carry trades.

Restored 2026-10-15 for trend_carry (deleted with gold_crude on 2026-10-14). Per order: Rs30 brokerage (Upstox Plus; Rs20 on Basic) + 18% GST.
CTT 0.01% on the SELL side (non-agri futures), stamp duty 0.002% on the BUY side, MCX exchange charge 0.0021% of turnover + 18% GST, SEBI Rs10 per
crore. Slippage: HALF_SPREAD_BP of each side's value (GOLDTEN's measured 09:00-09:40 spread was 2.3 bp, so ~1.1 bp a side).
"""
from __future__ import annotations

# rupee value per 1.00 of quoted price, per lot (MCX contract specifications)
LOT_UNITS = {"GOLDPETAL": 1, "GOLDTEN": 1, "GOLDM": 10, "SILVERMIC": 1, "SILVERM": 5, "CRUDEOILM": 10, "CRUDEOIL": 100, "NATGASMINI": 250,
             "NATURALGAS": 1250, "COPPER": 2500, "ZINCMINI": 1000, "ZINC": 5000, "ALUMINI": 1000, "ALUMINIUM": 5000, "LEADMINI": 1000, "LEAD": 5000}
BROKERAGE_PER_ORDER = 30.0
CTT_SELL, STAMP_BUY, EXCHANGE, SEBI, GST = 0.0001, 0.00002, 0.000021, 0.000001, 0.18
HALF_SPREAD_BP = 1.5


def lot_units(symbol: str) -> int:
    return LOT_UNITS.get(symbol.upper(), 1)


def compute_mcx_costs(symbol: str, direction: str, entry: float, exit_price: float, lots: int) -> dict:
    q = lots * lot_units(symbol)
    d = 1 if direction.lower() == "long" else -1
    ev, xv = entry * q, exit_price * q
    buy_v, sell_v = (ev, xv) if d == 1 else (xv, ev)
    brokerage = 2 * BROKERAGE_PER_ORDER
    exch = (ev + xv) * EXCHANGE
    sebi = (ev + xv) * SEBI
    ctt, stamp = sell_v * CTT_SELL, buy_v * STAMP_BUY
    gst = GST * (brokerage + exch + sebi)
    slip = (ev + xv) * HALF_SPREAD_BP / 1e4
    total = brokerage + exch + sebi + ctt + stamp + gst + slip
    gross = (exit_price - entry) * q * d
    return {"gross": round(gross, 2), "brokerage": round(brokerage, 2), "stt": round(ctt, 2), "ctt": round(ctt, 2), "stamp_duty": round(stamp, 2),
            "exchange_txn": round(exch, 2), "sebi": round(sebi, 2), "gst": round(gst, 2), "slippage": round(slip, 2), "total": round(total, 2),
            "net": round(gross - total, 2), "lots": lots, "qty": q}
