"""Live-vs-backtest exit replay at 1-minute resolution.  (docs/LIVE_VS_BACKTEST_EXITS.md)

    python3 -m engine.exit_replay [--markets commodity currency equity]

Takes each backtest's OWN entries and re-runs only the exit, three ways, on the 1-minute archive:

  bar      the backtests' logic on completed 5-minute bars: test the stop with the bar's adverse extreme, THEN move it with the bar's
           favourable extreme (a moved stop applies from the next bar on).
  old_live the live engine before 2026-10-05: a scan each minute on the still-forming 5-minute bar calling the single-bar exit functions,
           so a scan could test the bar's EARLIER low against a stop that a LATER high in the same bar had just raised.
  live     the live engine now: core.exits.*_bars fed exactly what a scan sees (completed bars + the forming bar), every minute.

Entries are identical across the three, so any difference is the exit mechanics alone. Settings come from .env (chop gates, exit mode).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

from core import exits
from core.paths import ARCHIVE_ROOT

SCAN_LAG = timedelta(seconds=20)          # a minute's bar is published ~15-20 s after it ends (measured 2026-10-05)
MODES = ("bar", "old_live", "live")


def load_1m(market: str, stem: str) -> pd.DataFrame:
    df = pd.read_csv(ARCHIVE_ROOT / market / f"{stem}_1minute.csv")
    df["ts"] = pd.to_datetime(df["timestamp"])
    return df.drop_duplicates("ts").sort_values("ts").set_index("ts")[["open", "high", "low", "close", "volume"]]


def bars5(m1: pd.DataFrame) -> pd.DataFrame:
    return m1.resample("5min", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def _bar(ts, rows: pd.DataFrame) -> dict:
    return {"timestamp": ts.isoformat(), "open": float(rows["open"].iloc[0]), "high": float(rows["high"].max()),
            "low": float(rows["low"].min()), "close": float(rows["close"].iloc[-1]), "volume": float(rows["volume"].sum())}


def replay(trade: dict, m1: pd.DataFrame, b5: pd.DataFrame, atr5: pd.Series, *, mode: str, kind: str,
           close_at: tuple[int, int], hold_s: float, max_bars: int = 400) -> tuple[float, str, pd.Timestamp]:
    """kind: 'fixed_tp' (MCX / currency fixed exit) or 'activation' (currency dynamic / equity). Returns (exit price, reason, exit time)."""
    T = pd.Timestamp(trade["entry_time"])
    d = 1 if trade["direction"] == "long" else -1
    entry = float(trade["entry_price"])
    pos = {"direction": trade["direction"], "entry_price": entry, "entry_time": (T + timedelta(minutes=5) + SCAN_LAG).to_pydatetime(),
           "entry_bar_ts": T.isoformat(), "tp": float(trade.get("tp", entry + 1e9 * d)), "be": float(trade.get("be", trade.get("activation_price"))),
           "current_stop": float(trade["sl"]), "best_price": entry, "stop_dist": float(trade["stop_dist"]), "armed_be": False,
           "activation_price": float(trade.get("activation_price", trade.get("be"))), "trail_mult": float(trade["trail_mult"]), "armed_trail": False}

    def single(bar, now, atr):
        return (exits.fixed_tp_breakeven_trail(pos, bar, now, close_at=close_at, max_hold_s=hold_s) if kind == "fixed_tp"
                else exits.activation_trail(pos, bar, atr, now, close_at=close_at, max_hold_s=hold_s))

    def atr_of(c):
        return float(atr5.get(pd.Timestamp(c["timestamp"]), pos["stop_dist"]))

    later = b5.loc[b5.index > T].iloc[:max_bars]
    day = T.strftime("%Y-%m-%d")
    done = [_bar(T, b5.loc[[T]])] if T in b5.index else []
    for B, row in later.iterrows():
        if B.strftime("%Y-%m-%d") != day:
            break
        if mode == "bar":
            dec = single({"timestamp": B.isoformat(), "high": row["high"], "low": row["low"], "close": row["close"]},
                         (B + timedelta(minutes=5) + SCAN_LAG).to_pydatetime(), float(atr5.get(B, pos["stop_dist"])))
            if dec is not None:
                return dec.price, dec.reason, B
            continue
        mins = m1.loc[(m1.index >= B) & (m1.index < B + timedelta(minutes=5))]
        for k in range(1, len(mins) + 1):
            now = (mins.index[k - 1] + timedelta(minutes=1) + SCAN_LAG).to_pydatetime()
            forming = _bar(B, mins.iloc[:k])
            if mode == "old_live":
                dec = single(forming, now, float(atr5.get(B - timedelta(minutes=5), pos["stop_dist"])))
            else:
                candles = done + [forming]
                dec = (exits.fixed_tp_breakeven_trail_bars(pos, candles, now, close_at=close_at, max_hold_s=hold_s) if kind == "fixed_tp"
                       else exits.activation_trail_bars(pos, candles, atr_of, now, close_at=close_at, max_hold_s=hold_s))
            if dec is not None:
                return dec.price, dec.reason, mins.index[k - 1]
        if len(mins):
            done.append(_bar(B, mins))
    last = later.iloc[-1] if len(later) else b5.loc[T]
    return float(last["close"]), "data_end", later.index[-1] if len(later) else T


def _summary(label: str, nets: list[float]) -> str:
    net = np.array(nets)
    w, l = net[net > 0], net[net <= 0]
    pf = w.sum() / -l.sum() if l.sum() < 0 else float("inf")
    return (f"  {label:9s} n={len(net):4d}  net {net.sum():+11,.0f}  win {100 * len(w) / max(len(net), 1):5.1f}%  "
            f"avg win {w.mean() if len(w) else 0:+8,.0f}  avg loss {l.mean() if len(l) else 0:+8,.0f}  PF {pf:5.2f}")


def _atr_commodity(b5: pd.DataFrame, sym: str) -> pd.Series:
    from markets.commodity.features import compute_commodity_features
    f = compute_commodity_features(b5.reset_index().rename(columns={"ts": "timestamp"}).assign(timestamp=lambda x: x["timestamp"].astype(str)), symbol=sym)
    return pd.Series(f["atr"].to_numpy(), index=b5.index)


def commodity_and_currency() -> None:
    import os
    from markets.commodity.scalping import backtest as cbt
    from markets.currency.scalping import backtest as ubt
    cases = [("SILVERMIC", "commodity", "SILVER", 7.9), ("CRUDEOILM", "commodity", "CRUDEOIL", 3.2), ("GOLDTEN", "commodity", "GOLD", 10.8), ("USDINR", "currency", "USDINR", 20.0)]
    total = {m: [] for m in MODES}
    for sym, market, stem, lev in cases:
        m1 = load_1m(market, stem)
        last_day = m1.index[-1].strftime("%Y-%m-%d")                 # replay only what the 1-minute archive covers
        with contextlib.redirect_stdout(io.StringIO()):
            if market == "currency":
                r = ubt.run_currency_backtest(symbols=[sym], capital=85906, risk_pct=10, leverage=lev, size_mode="margin", return_trades=True, to_date=last_day,
                                              exit_mode=os.environ.get("CURRENCY_EXIT_MODE", "fixed"), intraday_chop_window=int(os.environ.get("USDINR_INTRADAY_CHOP_WINDOW", "0")))
                cost = lambda d, e, x, lots, s=sym: ubt.compute_ncd_currency_costs(s, d, e, x, lots)
                kind, close_at = ("activation" if os.environ.get("CURRENCY_EXIT_MODE") == "dynamic" else "fixed_tp"), (16, 50)
            else:
                r = cbt.run_commodity_backtest(symbols=[sym], capital=85906, risk_pct=10, leverage=lev, size_mode="margin", us_session_only=False, return_trades=True, to_date=last_day,
                                               intraday_chop_window=int(os.environ.get("SILVERMIC_INTRADAY_CHOP_WINDOW", "0")) if sym == "SILVERMIC" else 0)
                cost = lambda d, e, x, lots, s=sym: cbt.compute_mcx_commodity_costs(s, d, e, x, lots)
                kind, close_at = "fixed_tp", (22, 45)
        trades = r.get("trade_list") or []
        b5 = bars5(m1)
        atr5 = _atr_commodity(b5, sym)
        print(f"{sym}: {len(trades)} backtest trades")
        for m in MODES:
            nets = [cost(t["direction"], float(t["entry_price"]), replay(t, m1, b5, atr5, mode=m, kind=kind, close_at=close_at, hold_s=80 * 60)[0], int(t["lots"]))["net"] for t in trades]
            total[m] += nets
            print(_summary(m, nets))
    print("commodity + currency:")
    for m in MODES:
        print(_summary(m, total[m]))


def equity(start: str = "2026-05-12", end: str = "2026-10-01") -> None:
    from markets.equity.experiments import multi_strategy_study as ms
    from markets.equity.features import compute_equity_features
    from markets.equity.scalping import backtest as eqbt
    from markets.equity.universe import NIFTY50_SYMBOLS
    per = {m: [] for m in MODES}
    for s in NIFTY50_SYMBOLS:
        if not (ARCHIVE_ROOT / "equity" / f"{s}_1minute.csv").exists():
            continue
        cands = eqbt._simulate_symbol_candidates(s, start, end, False, eqbt.ENTRY_THRESHOLDS, pullback_frac=0.15, pullback_through=0.02)
        if not cands:
            continue
        m1 = load_1m("equity", s)
        b5 = bars5(m1)
        f = compute_equity_features(b5.reset_index().rename(columns={"ts": "timestamp"}).assign(timestamp=lambda x: x["timestamp"].astype(str)))
        atr5 = pd.Series(f["atr"].to_numpy(), index=b5.index[-len(f):])
        for c in cands:
            for m in MODES:
                px, _, ts = replay(c, m1, b5, atr5, mode=m, kind="activation", close_at=(14, 55), hold_s=80 * 60)
                per[m].append({**c, "entry_time": str(c["entry_time"]), "exit_price": px, "exit_time": pd.Timestamp(ts).isoformat()})
    print(f"equity, 49 names, {start}..{end}, live portfolio rules (multi_strategy_study.pool):")
    for m in MODES:
        print(_summary(m, [t["net_pnl"] for t in ms.pool(per[m])]))


def main(argv=None) -> int:
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    load_dotenv(BACKEND_ROOT / ".env")
    logging.getLogger().setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["commodity", "equity"], choices=["commodity", "equity"])
    args = ap.parse_args(argv)
    if "commodity" in args.markets:
        commodity_and_currency()
    if "equity" in args.markets:
        equity()
    return 0


if __name__ == "__main__":
    sys.exit(main())
