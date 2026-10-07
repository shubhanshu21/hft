"""What would real fills have cost?  Paper trading fills at our own model's price; this measures, from real market data, the three costs a real
order would face, per market, in basis points per side.  (docs/EDGE_AUDIT.md: equity's edge has ~3-4 bp/side of cushion.)

    python3 -m engine.slippage_report [--days 30]

  spread     half of the quoted bid-ask spread (var/logs/spread_samples.csv, sampled live by the daemon) -- the cost of crossing it once
  drift      live entry price vs the close of the signal bar (the price the backtests assume): + = live paid more
  overshoot  for stop exits: how far past the stop price traded in the minute the stop was hit (1-minute candles) -- a stop-market order
             fills somewhere between the stop and there; reported as the minute's extreme (worst case) and its close (typical)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core.paths import DB_DIR, LOG_DIR

STOPS = ("initial_stop", "be_stop", "trail_stop")
_cache: dict = {}


def market_of(sym: str) -> str:
    from markets.equity.universe import NIFTY50_SYMBOLS
    return "equity" if sym in NIFTY50_SYMBOLS else ("currency" if sym in ("USDINR", "EURINR", "GBPINR", "JPYINR") else "commodity")


def candles(broker, ikey: str, day: str, minutes: int) -> pd.DataFrame:
    k = (ikey, day, minutes)
    if k not in _cache:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            raw = broker.get_intraday_candles(ikey, "minutes", minutes) if day == today else broker.get_historical_candles(ikey, "minutes", minutes, to_date=day, from_date=day)
        except Exception:
            raw = None
        df = pd.DataFrame(raw or [])
        if not df.empty:
            df["ts"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("ts").sort_index()
        _cache[k] = df
    return _cache[k]


def report(days: int = 30, broker=None) -> str:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_DIR / "paper_trading.db")
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""SELECT t.symbol, t.direction, t.entry_price, t.exit_price, t.entry_dt, t.exit_dt, t.exit_reason, p.state, p.instrument_key
                                            FROM trades t LEFT JOIN positions p ON p.position_id = t.position_id WHERE t.entry_dt >= ? ORDER BY t.entry_dt""", (since,))]
    con.close()
    if broker is None:
        from services.broker.upstox_broker import UpstoxBroker
        from engine.config import UpstoxConfig
        broker = UpstoxBroker(access_token=UpstoxConfig.ACCESS_TOKEN, dry_run=True)
    per = {}
    for r in rows:
        m = market_of(r["symbol"])
        d = 1 if r["direction"] == "long" else -1
        st = json.loads(r["state"] or "{}")
        bar = st.get("entry_bar_ts")
        e = per.setdefault(m, {"drift": [], "over_worst": [], "over_close": [], "n": 0})
        e["n"] += 1
        if bar and r["instrument_key"]:
            c5 = candles(broker, r["instrument_key"], bar[:10], 5)
            ts = pd.Timestamp(bar)
            if not c5.empty and ts in c5.index:
                close = float(c5.loc[ts, "close"])
                e["drift"].append((r["entry_price"] - close) * d / close * 1e4)
        if r["exit_reason"] in STOPS and r["instrument_key"]:
            c1 = candles(broker, r["instrument_key"], r["exit_dt"][:10], 1)
            t = pd.Timestamp(r["exit_dt"]).floor("min")
            if not c1.empty and t in c1.index:
                stop = float(r["exit_price"])
                worst = (stop - float(c1.loc[t, "low"])) if d == 1 else (float(c1.loc[t, "high"]) - stop)
                typ = (stop - float(c1.loc[t, "close"])) if d == 1 else (float(c1.loc[t, "close"]) - stop)
                e["over_worst"].append(max(worst, 0.0) / stop * 1e4)
                e["over_close"].append(max(typ, 0.0) / stop * 1e4)
    spread = {}
    try:
        sp = pd.read_csv(LOG_DIR / "spread_samples.csv", parse_dates=["timestamp"])
        sp = sp[sp["timestamp"].astype(str) >= since]
        for sym, g in sp.groupby("symbol"):
            spread.setdefault(market_of(sym), []).append(float(g["spread_pct"].median()) * 100 / 2)      # % -> bp, halved
    except (FileNotFoundError, KeyError):
        pass
    med = lambda v: f"{np.median(v):5.2f}" if len(v) else "  n/a"
    lines = [f"Execution cost evidence, last {days} days ({len(rows)} closed paper trades), bp per side:",
             f"  {'market':10s} {'trades':>6s} | {'half-spread':>11s} | {'entry drift (median, n)':>24s} | {'stop overshoot typical / worst (median, n)':>42s}"]
    for m in sorted(set(per) | set(spread)):
        e = per.get(m, {"drift": [], "over_worst": [], "over_close": [], "n": 0})
        lines.append(f"  {m:10s} {e['n']:6d} | {med(spread.get(m, []))} (median) | {med(e['drift'])} ({len(e['drift'])})"
                     f"{'':10s} | {med(e['over_close'])} / {med(e['over_worst'])} ({len(e['over_close'])})")
    lines.append("  Equity's backtested edge survives +2 bp/side of extra cost (+2.28 lakh over 4 years) but not +5 bp (-3.99 lakh): docs/EDGE_AUDIT.md.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    load_dotenv(BACKEND_ROOT / ".env")
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    print(report(a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
