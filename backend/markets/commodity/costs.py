"""MCX futures costs (Upstox Plus) for the commodity contracts we trade, and their lot units.

Per order: Rs30 brokerage (Upstox Plus; Rs20 on Basic) + 18% GST. CTT 0.01% on the SELL side (non-agri futures), stamp duty 0.002% on the BUY side,
MCX exchange charge 0.0021% of turnover + 18% GST, SEBI Rs10 per crore. Slippage is NOT included: the backtester's slippage setting moves fills.
"""
from __future__ import annotations

# rupee value per 1.00 of quoted price, per lot: GOLDPETAL is quoted per gram for a 1 g lot; CRUDEOILM per barrel for a 10-barrel lot
LOT_UNITS = {"GOLDPETAL": 1, "CRUDEOILM": 10}
BROKERAGE_PER_ORDER = 30.0
CTT_SELL, STAMP_BUY, EXCHANGE, SEBI, GST = 0.0001, 0.00002, 0.000021, 0.000001, 0.18


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
    total = brokerage + exch + sebi + ctt + stamp + gst
    gross = (exit_price - entry) * q * d
    return {"gross": round(gross, 2), "brokerage": round(brokerage, 2), "ctt": round(ctt, 2), "stamp_duty": round(stamp, 2), "exchange_txn": round(exch, 2),
            "sebi": round(sebi, 2), "gst": round(gst, 2), "slippage": 0.0, "total": round(total, 2), "net": round(gross - total, 2), "lots": lots, "qty": q}
