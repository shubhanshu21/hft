"""Cost-model drift monitor: our charges versus Upstox's own brokerage calculator.

    python3 -m engine.cost_drift [--symbols CRUDEOILM SILVERMIC USDINR RELIANCE] [--no-telegram]

Brokerage, STT/CTT, exchange and SEBI fees, GST and stamp duty are set by Upstox, the exchanges and the government, and change without notice
(2026-09-24: Upstox charged min(0.06%, Rs30) per order while the model had min(0.05%, Rs20), understating every trade's cost by ~Rs23.6 for
weeks). Upstox exposes the exact figure (`ChargeApi.get_brokerage`), so each night, and at daemon start, we price a one-lot round trip at the
live price with BOTH and alert when the model is off. A model that is CHEAPER than Upstox by more than UNDER_TOL is the dangerous direction
(profits are overstated); dearer by more than OVER_TOL just means we are being conservative but should be re-fitted.
"""
from __future__ import annotations

import argparse
import sys

UNDER_TOL = 0.03          # model cheaper than Upstox by more than 3%
OVER_TOL = 0.10           # model dearer than Upstox by more than 10%


def upstox_round_trip(api, key: str, qty_units: int, price: float) -> float | None:
    """Upstox's total charges for BUY then SELL of `qty_units` at `price`, intraday (MIS)."""
    try:
        legs = [api.get_brokerage(key, qty_units, "I", side, price, "2.0").data.charges.total for side in ("BUY", "SELL")]
        return float(sum(legs))
    except Exception:
        return None


def model_round_trip(sym: str, market: str, lots: int, price: float) -> float:
    """Our model's statutory charges (slippage excluded) for a flat round trip at `price`."""
    import core.slippage as sl
    from unittest.mock import patch
    with patch.object(sl, "_cache", {}), patch.object(sl, "_cache_loaded_at", 1e18):
        if market == "equity":
            from markets.equity.costs import compute_nse_equity_costs
            r = compute_nse_equity_costs("long", price, price, lots, symbol=sym)
        elif market == "currency":
            from markets.currency.costs import compute_ncd_currency_costs
            r = compute_ncd_currency_costs(sym, "long", price, price, lots)
        else:
            from markets.commodity.costs import compute_mcx_commodity_costs
            r = compute_mcx_commodity_costs(sym, "long", price, price, lots)
    return float(r.get("total_friction", r.get("total")) - r.get("slippage", 0.0))


def check(broker, keys: dict[str, str], market_of, lots_for=None, price_of=None) -> list[dict]:
    """One row per symbol: {symbol, upstox, model, ratio, status}. Symbols Upstox cannot price are reported as 'no-data', never 'ok'."""
    import upstox_client
    api = upstox_client.ChargeApi(broker._api_client)
    rows = []
    for sym, key in keys.items():
        market = market_of(sym)
        lots = (lots_for or (lambda s: 100 if market == "equity" else 1))(sym)
        price = (price_of or broker.get_ltp)(key)
        if not price:
            rows.append({"symbol": sym, "status": "no-data", "why": "no price"})
            continue
        if market == "equity":
            units = lots
        else:
            from markets.commodity.costs import get_contract_multiplier as com
            from markets.currency.costs import get_contract_multiplier as cur
            units = lots * (cur(sym) if market == "currency" else com(sym))
        up = upstox_round_trip(api, key, units, float(price))
        if not up:
            rows.append({"symbol": sym, "status": "no-data", "why": "Upstox brokerage calculator unavailable"})
            continue
        mod = model_round_trip(sym, market, lots, float(price))
        ratio = mod / up
        status = "UNDERSTATES" if ratio < 1 - UNDER_TOL else "overstates" if ratio > 1 + OVER_TOL else "ok"
        rows.append({"symbol": sym, "upstox": round(up, 2), "model": round(mod, 2), "ratio": round(ratio, 3), "status": status})
    return rows


def format_rows(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        if "ratio" in r:
            lines.append(f"  {r['symbol']:<10s} Upstox Rs{r['upstox']:8.2f}  model Rs{r['model']:8.2f}  model/Upstox {r['ratio']:.3f}  {r['status']}")
        else:
            lines.append(f"  {r['symbol']:<10s} {r['status']}: {r['why']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["CRUDEOILM", "SILVERMIC", "USDINR", "RELIANCE"])
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args(argv)
    from engine.live_dryrun import SYMBOL_MAP, _get_market
    from markets.commodity.data import _get_broker
    broker = _get_broker()
    rows = check(broker, {s: SYMBOL_MAP[s] for s in args.symbols if SYMBOL_MAP.get(s)}, _get_market)
    text = format_rows(rows)
    print("cost model vs Upstox brokerage calculator (one-lot flat round trip, statutory charges only):\n" + text)
    bad = [r for r in rows if r["status"] in ("UNDERSTATES", "overstates")]
    if bad and not args.no_telegram:
        from services.utils import telegram
        telegram.send("💸 <b>COST MODEL DRIFT</b> — our charges differ from Upstox's calculator:\n" + format_rows(bad) +
                      "\nUpdate UPSTOX_BROKERAGE_* / statutory rates in markets/*/costs.py and re-run the backtests.")
    return 1 if any(r["status"] == "UNDERSTATES" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
