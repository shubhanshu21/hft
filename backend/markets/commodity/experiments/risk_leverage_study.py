"""Risk % x leverage backtest under what Upstox REALLY allows (real per-lot margin, unaffordable symbols skipped).

Effective leverage = min(configured, real Upstox leverage from engine/margin_rates.py). Sizing returns 0 lots when one lot does not fit
the capital. Commodity/currency: each symbol on its own Rs100,000, 2026-05-18..2026-09-23 in two halves. Equity: the whole NIFTY50 universe
on ONE shared Rs100,000 portfolio (concurrent-position cap as live).

    python3 -m markets.commodity.experiments.risk_leverage_study
"""
from __future__ import annotations

import contextlib
import io

from engine import margin_rates
from markets.commodity.scalping.backtest import run_commodity_backtest
from markets.currency.scalping.backtest import run_currency_backtest
from markets.equity.scalping.backtest import run_equity_backtest

CAPITAL = 100000.0
CONFIGS = [(4.0, 5.0, "risk 4,  configured 5x"), (10.0, 5.0, "risk 10, configured 5x"),
           (4.0, 99.0, "risk 4,  max Upstox allows"), (10.0, 99.0, "risk 10, max Upstox allows")]
HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23"), "all": ("2026-05-18", "2026-09-23")}


def _stats(trades: list[dict]) -> dict:
    net = [t["net_pnl"] for t in trades]
    w, l = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
    peak = eq = CAPITAL
    dd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return {"n": len(net), "win": 100 * sum(x > 0 for x in net) / len(net) if net else 0.0, "net": sum(net), "pf": w / l if l else float("inf"),
            "dd": dd, "worst": min(net) if net else 0.0, "size": sum(t.get("lots", t.get("qty", 0)) for t in trades) / len(trades) if trades else 0.0}


def _line(label: str, lev: float, s: dict) -> str:
    if not s["n"]:
        return f"   {label:28s} eff.lev {lev:5.1f}x  NO TRADES (one lot does not fit the capital)"
    pf = "  inf" if s["pf"] == float("inf") else f"{s['pf']:5.2f}"
    return (f"   {label:28s} eff.lev {lev:5.1f}x  n={s['n']:4d} win={s['win']:3.0f}% PF={pf} net={s['net']:+9.0f} maxDD={s['dd']:5.1f}% "
            f"worst={s['worst']:+8.0f} avg size={s['size']:6.1f}")


def _run(kind, sym, risk, lev, a, b):
    kw = dict(symbols=[sym] if isinstance(sym, str) else sym, capital=CAPITAL, risk_pct=risk, leverage=lev, from_date=a, to_date=b, return_trades=True)
    with contextlib.redirect_stdout(io.StringIO()):
        if kind == "commodity":
            r = run_commodity_backtest(size_mode="margin", **kw)
        elif kind == "currency":
            r = run_currency_backtest(size_mode="margin", **kw)
        else:
            r = run_equity_backtest(**kw)
    return _stats(r.get("trade_list") or [])


def main() -> None:
    for kind, syms in (("commodity", ["CRUDEOILM", "GOLDM", "SILVER"]), ("currency", ["USDINR"])):
        for sym in syms:
            real = margin_rates.real_leverage(sym)
            m = margin_rates.margin_per_unit(sym)
            print(f"\n=== {sym}: Upstox margin Rs{m:,.0f}/lot = {real:.1f}x real leverage" + ("  (ONE LOT DOES NOT FIT Rs100,000)" if m > CAPITAL else ""))
            for half, (a, b) in HALVES.items():
                print(f"  -- {half} ({a}..{b})")
                for risk, cfg, name in CONFIGS:
                    lev = margin_rates.cap_leverage(sym, cfg)
                    print(_line(name, lev, _run(kind, sym, risk, lev, a, b)))
    print("\n=== EQUITY, whole NIFTY50 universe on one shared Rs100,000 (Upstox MIS = 5.0x for every stock)")
    from markets.equity.universe import NIFTY50_SYMBOLS
    for risk, cfg, name in CONFIGS:
        lev = min(cfg, 5.0)
        print(_line(name, lev, _run("equity", list(NIFTY50_SYMBOLS), risk, lev, None, None)))


if __name__ == "__main__":
    main()
