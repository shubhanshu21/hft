"""Backtest service for the web frontend (the "New backtest" page).  Run:  python3 -m engine.backtest_api   (systemd: hft-backtest-api.service)

    GET  /api/strategies         what can be run: market, name, timeframe, whether the web backtester supports it, symbols
    GET  /api/backtests          saved runs, newest first (summary only)
    GET  /api/backtests/<id>     one run: status, params, and the full replay result when finished
    POST /api/backtests          start a run (JSON body; optional label)
    POST /api/backtests/<id>/label   rename a saved run (JSON body with label)
    GET  /api/paper              the paper-trading account (read-only): balance, open positions, trades, daily account value
    DELETE /api/backtests/<id>   delete one saved run;  DELETE /api/backtests  delete every saved run (the one running now is kept)

It only reads archived candles and runs the strategy code; it never talks to Upstox, touches the paper-trading database or places an order.
Fees (the strategy's cost model) and slippage are always charged. One run at a time; results are saved in var/backtests/<id>.json.
Binds to 127.0.0.1 -- nginx publishes /api/. No login: a run only uses CPU, so it is limited to one at a time, one start per address every
COOLDOWN_S seconds, and the newest MAX_SAVED runs are kept.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from core import registry
from core.backtester import BacktestError, run_backtest
from core.paths import ARCHIVE_ROOT, BACKEND_ROOT, VAR_DIR
from markets.equity.universe import NIFTY50_SYMBOLS

RUNS_DIR = VAR_DIR / "backtests"
CURRENCY = ["USDINR", "EURINR", "GBPINR"]
LIMITS = {"capital": (10_000, 1_000_000_000), "slippage_bp": (0, 100), "risk_pct": (0.1, 10), "max_positions": (1, 10), "leverage": (1, 40),
          "trail_stop_pct": (0, 30), "trail_after_pct": (0, 50), "profit_trail_pct": (0, 20)}
COOLDOWN_S = 5
MAX_SAVED = 200
_lock = threading.Lock()
_running: dict | None = None
_last_start: dict[str, float] = {}


def universe(market: str) -> list[str]:
    return list(NIFTY50_SYMBOLS) if market == "equity" else CURRENCY


def catalog() -> list[dict]:
    from markets.commodity.strategies.tf_monthly.trend_carry import strategy as tc
    mcx = {"market": "commodity", "name": "trend_carry", "timeframe": "1 months", "supported": True, "symbols": sorted(set(tc.TRADE.values())),
           "summary": (tc.__doc__ or "").strip().split("\n")[0], "intraday": False, "default_risk_pct": None, "monthly_rank": True,
           "exposure": float(os.environ.get("MCX_TREND_CARRY_EXPOSURE", 1.5)), "exposure_min": tc.EXPOSURE_MIN, "exposure_max": tc.EXPOSURE_MAX}
    from markets.crypto.strategies.tf_5min.dip_reversal import strategy as dr
    dip_reversal = {"market": "crypto", "name": "dip_reversal", "timeframe": "5 minutes", "supported": True, "symbols": dr.available(), "crypto": True, "scalp": True,
                     "default_symbols": list(dr.COINS), "summary": (dr.__doc__ or "").strip().split("\n")[0], "intraday": True, "default_risk_pct": dr.RISK_PCT * 100,
                     "exposure": 3.0, "exposure_min": 1.0, "exposure_max": 5.0}
    return [{"market": m, "name": n, "timeframe": f"{s.timeframe[1]} {s.timeframe[0]}", "supported": tuple(s.timeframe) == ("days", 1), "symbols": list(getattr(s, "universe", None) or universe(m)),
             "summary": (s.__doc__ or "").strip().split("\n")[0], "intraday": s.intraday,
             "default_risk_pct": getattr(s, "default_risk_pct", None),
             "default_symbols": [x for x in (getattr(s, "universe", None) or universe(m)) if s.trades(x)]}     # what its paper trading uses
            for (m, n), s in sorted(registry.discover().items())] + [mcx, dip_reversal]


def prune() -> None:
    files = sorted(RUNS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[MAX_SAVED:]:
        f.unlink(missing_ok=True)


def save(run: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_DIR / f"{run['id']}.json.tmp"
    tmp.write_text(json.dumps(run, separators=(",", ":"), default=str))
    tmp.replace(RUNS_DIR / f"{run['id']}.json")


def load(run_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        return None
    f = RUNS_DIR / f"{run_id}.json"
    return json.loads(f.read_text()) if f.exists() else None


def summaries() -> list[dict]:
    out = []
    for f in RUNS_DIR.glob("*.json") if RUNS_DIR.exists() else []:
        try:
            r = json.loads(f.read_text())
        except ValueError:
            continue
        out.append({k: r.get(k) for k in ("id", "status", "created", "market", "strategy", "params", "error", "label")} | {"metrics": (r.get("result") or {}).get("metrics")})
    if _running:
        out = [r for r in out if r["id"] != _running["id"]] + [{k: _running.get(k) for k in ("id", "status", "created", "market", "strategy", "params", "error", "progress")} | {"metrics": None}]
    return sorted(out, key=lambda r: r["created"] or "", reverse=True)


def validate(body: dict) -> dict:
    market, name = str(body.get("market", "")), str(body.get("strategy", ""))
    known = {(r["market"], r["name"]): r for r in catalog()}
    if (market, name) not in known:
        raise ValueError("Unknown market or strategy")
    if not known[(market, name)]["supported"]:
        raise ValueError(f"{market}/{name} uses {known[(market, name)]['timeframe']} bars; the web backtester runs daily-bar strategies")
    if known[(market, name)].get("crypto"):                      # calm_coins: 5 coins' futures; leverage 1-3x and the tax rule are the choices
        row = known[(market, name)]
        chosen = [str(s).upper() for s in body.get("symbols") or row["symbols"]]
        if any(s not in row["symbols"] for s in chosen):
            raise ValueError("Coins must come from the list")
        p = {"symbols": chosen if (row.get("scalp") or name == "trend_breakout") else list(row["symbols"]), "slippage_bp": 0.0, "pyramid_pct": None,
             "tax": {True: "strict", False: "none"}.get(body.get("tax", "business"), body.get("tax"))}
        if p["tax"] not in ("strict", "business", "none"):
            raise ValueError("Tax must be strict, business or none")
        lo, hi = LIMITS["capital"]; p["capital"] = float(body.get("capital", 100_000))
        if not lo <= p["capital"] <= hi:
            raise ValueError(f"capital must be between {lo} and {hi}")
        p["leverage"] = float(body.get("exposure", 1.0))
        if not row["exposure_min"] <= p["leverage"] <= row["exposure_max"]:
            raise ValueError(f"Leverage must be between {row['exposure_min']:g}x and {row['exposure_max']:g}x")
        if row.get("default_risk_pct") is not None:                 # dip_reversal: its own risk-per-trade knob, as a % (e.g. 8 = 8%)
            lo, hi = 0.1, 20                                         # wider than the stock knob: the shipped default is 13%
            p["risk_pct"] = float(body.get("risk_pct", row["default_risk_pct"]))
            if not lo <= p["risk_pct"] <= hi:
                raise ValueError(f"Risk per trade must be between {lo}% and {hi}%")
        for key in ("start", "end"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key, ""))):
                raise ValueError("Dates must look like 2024-01-31")
            p[key] = body[key]
        return {"market": market, "strategy": name, "params": p}
    if known[(market, name)].get("auto_universe"):              # breakout_pair picks from the day's 200 most-traded NSE stocks itself
        p = {"symbols": [], "leverage": 1.0, "pyramid_pct": None, "max_positions": 8.0}
        for key in ("capital", "slippage_bp", "risk_pct"):
            lo, hi = LIMITS[key]
            v = float(body.get(key, {"capital": 100_000, "slippage_bp": 5, "risk_pct": 2}[key]))
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            p[key] = v
        for key in ("start", "end"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key, ""))):
                raise ValueError("Dates must look like 2024-01-31")
            p[key] = body[key]
        return {"market": market, "strategy": name, "params": p}
    allowed = known[(market, name)]["symbols"]                     # a strategy may name its own instruments (trend_ema_cross: NIFTY 50 + US/HK ETFs)
    symbols = [str(s).upper() for s in body.get("symbols") or allowed]
    if not symbols or any(s not in allowed for s in symbols):
        raise ValueError("Symbols must come from the market's list")
    p = {"symbols": symbols}
    for key, (lo, hi) in LIMITS.items():
        v = float(body.get(key, {"capital": 100_000, "slippage_bp": 5, "risk_pct": 2, "max_positions": 3, "leverage": 1, "trail_stop_pct": 0, "trail_after_pct": 0, "profit_trail_pct": 0}[key]))
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        p[key] = v
    if market == "commodity":                                    # trend_carry ranks the chosen commodities together each month
        if len(symbols) < 2:
            raise ValueError("Pick at least 2 commodities: the strategy ranks them against each other")
        row = known[(market, name)]
        ex = float(body.get("exposure", row["exposure"]))
        if not row["exposure_min"] <= ex <= row["exposure_max"]:
            raise ValueError(f"Exposure must be between {row['exposure_min']}x and {row['exposure_max']}x (the most Upstox margin allows)")
        p["leverage"] = ex
        p["slippage_bp"] = 0.0                                   # the MCX cost model already charges a half-spread
        p["pyramid_pct"] = None
        for key in ("start", "end"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key, ""))):
                raise ValueError("Dates must look like 2024-01-31")
            p[key] = body[key]
        return {"market": market, "strategy": name, "params": p}
    # Leverage is not the user's choice: delivery shares are bought with the account's own cash (1x); futures use the margin Upstox really
    # charges (engine/margin_rates, refreshed by the paper trader), the lowest of the chosen symbols. The strategy's own caps still apply.
    strat = registry.get(market, name)
    if not getattr(strat, "uses_leverage", False):
        p["leverage"] = 1.0
    else:
        from engine import margin_rates
        p["leverage"] = round(min(min(margin_rates.real_leverage(s) or 40.0 for s in symbols), LIMITS["leverage"][1]), 1)
    if market == "currency":
        p["slippage_bp"] = 0.0              # the currency cost model already charges the measured bid-ask spread; extra slippage would count it twice
    pyr = body.get("pyramid_pct")
    if pyr in (None, ""):
        p["pyramid_pct"] = None                                         # the strategy's own setting
    else:
        pyr = float(pyr)
        if not 0 <= pyr <= 30:
            raise ValueError("pyramid_pct must be between 0 and 30")
        p["pyramid_pct"] = pyr
    for key in ("start", "end"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key, ""))):
            raise ValueError("Dates must look like 2024-01-31")
        p[key] = body[key]
    return {"market": market, "strategy": name, "params": p}


IST = timezone(timedelta(hours=5, minutes=30))
_LIVE_TTL = 30.0
_live_cache: dict = {"at": 0.0, "keys": (), "prices": {}}


def _live_prices(keys: list[str]) -> dict:
    """Upstox market quote per instrument key -> {"ltp", "trade_time" (IST, of the last trade), "bid", "ask"}: one batch call at most every
    30 s (the page polls each minute; Upstox allows 2,000 calls per 30 minutes). Thin contracts (EURINR / GBPINR futures trade a few hundred
    lots a day) can keep the same last price for many minutes while the bid and ask still move. Empty when there is no valid token or
    Upstox is unreachable; the caller then falls back to the last daily close."""
    keys = tuple(sorted(set(keys)))
    if not keys:
        return {}
    now = time.time()
    with _lock:
        if _live_cache["keys"] == keys and now - _live_cache["at"] < _LIVE_TTL:
            return _live_cache["prices"]
    prices: dict = {}
    try:
        import requests
        from engine.config import _load_access_token
        token = _load_access_token()
        if token:
            r = requests.get("https://api.upstox.com/v2/market-quote/quotes", params={"instrument_key": ",".join(keys)},
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            for v in (r.json().get("data") or {}).values():
                depth = v.get("depth") or {}
                bid = next((x["price"] for x in depth.get("buy") or [] if x.get("price")), None)
                ask = next((x["price"] for x in depth.get("sell") or [] if x.get("price")), None)
                ltt = v.get("last_trade_time")
                prices[v.get("instrument_token")] = {
                    "ltp": v.get("last_price") or None, "bid": bid, "ask": ask,
                    "trade_time": datetime.fromtimestamp(int(ltt) / 1000, IST).isoformat(timespec="seconds") if ltt else None}
    except Exception:
        traceback.print_exc()
    with _lock:
        _live_cache.update(at=now, keys=keys, prices=prices)
    return prices


def _last_close(symbol: str, market: str) -> tuple[float | None, str | None]:
    f = ARCHIVE_ROOT / ("currency" if market == "currency" else "equity") / (f"{symbol}_daily_adj.csv" if market == "currency" else f"{symbol}_daily.csv")
    if market == "equity" and not f.exists():                          # breakout_pair trades beyond the NIFTY list: all-NSE daily archive
        f = ARCHIVE_ROOT / "equity_all" / f"{symbol}_daily.csv.gz"
    try:
        last = pd.read_csv(f).iloc[-1]
        return float(last["close"]), str(last["timestamp"])[:10]
    except Exception:
        return None, None


def _value_position(p: dict, live: dict) -> None:
    """Price an open paper position now: live price (else the last daily close), profit before fees, what selling now would net after the
    strategy's own cost model (the same call the paper trader makes when it closes), money at risk to the stop and the margin it blocks."""
    from core.symbols import market_of
    state = json.loads(p.pop("state") or "{}")
    market = market_of(p["symbol"])
    strat = registry.get(market, p["strategy"])
    lot = int(state.get("lot_size") or (strat.lot_size(p["symbol"]) if strat else 1) or 1)
    sign = -1 if p["direction"] == "short" else 1
    q = live.get(p.get("instrument_key")) or {}
    price, source, as_of = q.get("ltp"), "live", q.get("trade_time")
    p.update(bid=q.get("bid"), ask=q.get("ask"), checked_at=datetime.now(IST).isoformat(timespec="seconds") if q else None)
    if price is None:
        price, as_of = _last_close(p["symbol"], market)
        source = "last close"
    p.update(market=market, lot_size=lot, units=p["qty"] * lot, price_now=price, price_source=source if price is not None else None, price_time=as_of,
             margin_used=round(float(state.get("margin_used") or p["qty"] * lot * p["entry_price"]), 2),
             risk_to_stop=round((p["entry_price"] - p["current_stop"]) * sign * p["qty"] * lot, 2))
    p.pop("instrument_key", None)
    if price is None:
        p.update(profit_now=None, fees_if_sold=None, net_if_sold=None, move_pct=None)
        return
    p["profit_now"] = round((price - p["entry_price"]) * sign * p["qty"] * lot, 2)
    p["move_pct"] = round((price / p["entry_price"] - 1) * 100 * sign, 3)
    try:
        c = strat.costs(p["symbol"], p["direction"], p["entry_price"], price, p["qty"])
        p["net_if_sold"], p["fees_if_sold"] = round(c["net"], 2), round(c["gross"] - c["net"], 2)
    except Exception:
        p["net_if_sold"] = p["fees_if_sold"] = None


PAPER_ACCOUNTS = [("MCX_TREND_CARRY", "MCX trend + carry"), ("CRYPTO_DIP_REVERSAL", "Crypto dip-reversal")]
ACCOUNT_STRATEGIES = {"MCX_TREND_CARRY": "trend_carry", "CRYPTO_DIP_REVERSAL": "dip_reversal"}


MARKET_HOURS = {"MCX_TREND_CARRY": ("09:00", None, "MCX")}


def market_status(account: str) -> tuple[bool, str]:
    """Is this account's market open right now (IST)? Crypto never closes; NSE/MCX: weekdays, not on a trading holiday, within session hours.
    MCX closes 23:30 while the US is on summer time, 23:55 otherwise. Holidays: the NSE/CDS list (an MCX evening session on an NSE holiday shows closed)."""
    if account in ("CRYPTO_CALM", "CRYPTO_DIP_REVERSAL"):
        return True, "Crypto trades 24 hours, every day"
    start, end, name = MARKET_HOURS.get(account, ("09:15", "15:30", "NSE"))
    now = datetime.now(IST)
    if end is None:
        from zoneinfo import ZoneInfo
        end = "23:30" if datetime.now(ZoneInfo("America/New_York")).dst() else "23:55"
    hours = f"{name} {start}-{end} IST, Mon-Fri"
    if now.weekday() >= 5:
        return False, f"{hours}: closed for the weekend"
    try:
        from services.utils.market_holidays import get_trading_holidays
        if now.date() in get_trading_holidays():
            return False, f"{hours}: closed today (trading holiday)"
    except Exception:
        pass
    is_open = start <= now.strftime("%H:%M") < end
    return is_open, f"{hours}: {'open now' if is_open else 'closed now'}"


def _value_mcx(p: dict, live: dict) -> None:
    """An open MCX trend_carry position: quantity is lots of the mini contract (engine/mcx_trend_carry.py)."""
    from markets.commodity.costs import LOT_SIZE_TEXT, compute_mcx_costs
    st = json.loads(p.pop("state") or "{}")
    fam = st.get("family", p["symbol"]); units = int(st.get("lot_units", 1)); per, unit = LOT_SIZE_TEXT.get(fam, (1, "units"))
    q = live.get(p.get("instrument_key")) or {}
    price = q.get("ltp")
    p.update(market="commodity", lot_size=units, units=p["qty"] * units, size_text=f"{p['qty']} lot{'s' if p['qty'] != 1 else ''} · {p['qty'] * per:g} {unit}",
             price_now=price, price_source="live" if price else None, price_time=q.get("trade_time"), bid=q.get("bid"), ask=q.get("ask"),
             checked_at=datetime.now(IST).isoformat(timespec="seconds") if q else None, margin_used=float(st.get("margin_used") or 0.0), risk_to_stop=None)
    p.pop("instrument_key", None)
    if price is None:
        p.update(profit_now=None, fees_if_sold=None, net_if_sold=None, move_pct=None); return
    c = compute_mcx_costs(fam, "long", p["entry_price"], price, p["qty"])
    p.update(profit_now=round(c["gross"], 2), net_if_sold=round(c["net"], 2), fees_if_sold=round(c["total"], 2), move_pct=round((price / p["entry_price"] - 1) * 100, 3))


def _value_crypto(p: dict, live: dict) -> None:
    """An open crypto position in USD, profit in rupees at the USD/INR reference rate, 'if sold now'."""
    from engine.crypto_dip_reversal import fetch_live_prices
    st = json.loads(p.get("state") or "{}")
    q = live.get("crypto") or live.setdefault("crypto", fetch_live_prices())
    price, fx = q.get("prices", {}).get(p["symbol"]), q.get("usdtinr", 92.5)
    size = f"{p['direction']} · stop {p.get('current_stop', 0):,.4g}"
    p.update(market="crypto", lot_size=1, units=p["qty"], size_text=f"{p['qty']:g} {p['symbol']} · {size}", price_now=price,
             price_source="live" if price else None, price_time=q.get("timestamp"), bid=None, ask=None, checked_at=q.get("timestamp"),
             margin_used=float(st.get("margin_inr") or 0.0), risk_to_stop=None)
    p.pop("state", None); p.pop("instrument_key", None)
    if not (price and fx):
        p.update(profit_now=None, fees_if_sold=None, net_if_sold=None, move_pct=None); return
    entry = float(p["entry_price"])
    qty = float(p["qty"])
    d = 1 if p["direction"] == "long" else -1
    gross = (price - entry) * qty * d * fx
    fee = (price + entry) * qty * 0.0002 * fx
    net = gross - fee
    p.update(profit_now=round(gross, 2), net_if_sold=round(net, 2), fees_if_sold=round(fee, 2), move_pct=round((price / entry - 1) * 100 * d, 3),
             pretax_if_sold=round(net, 2))


def paper_summary(account: str = "CRYPTO_DIP_REVERSAL") -> dict:
    """A paper-trading account, read-only (SQLite opened mode=ro). Open positions are valued at the live Upstox price (else the last close)."""
    import sqlite3
    from core.paths import DB_DIR
    path = DB_DIR / "paper_trading.db"
    if not path.exists():
        return {"available": False}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if account not in dict(PAPER_ACCOUNTS):
            account = "CRYPTO_DIP_REVERSAL"
        tabs = []
        for aid, label in PAPER_ACCOUNTS:
            r = con.execute("SELECT initial_capital, current_capital FROM accounts WHERE account_id = ?", (aid,)).fetchone()
            n = con.execute("SELECT COUNT(*) FROM positions WHERE account_id = ? AND status = 'OPEN'", (aid,)).fetchone()[0]
            if r:
                mo, mtext = market_status(aid)
                tabs.append({"id": aid, "label": label, "strategies": ACCOUNT_STRATEGIES.get(aid, ""), "start": r[0], "booked": r[1], "open": n,
                             "market_open": mo, "market_text": mtext})
        acc = con.execute("SELECT * FROM accounts WHERE account_id = ?", (account,)).fetchone()
        pos = [dict(r) for r in con.execute("SELECT symbol, direction, qty, entry_price, current_stop, best_price, entry_time, strategy, instrument_key, state FROM positions "
                                              "WHERE account_id = ? AND status = 'OPEN' ORDER BY entry_time", (account,))]
        trades = [dict(r) for r in con.execute("SELECT symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, exit_reason, gross_pnl, total_friction, net_pnl, strategy "
                                                 "FROM trades WHERE account_id = ? ORDER BY exit_dt DESC LIMIT 500", (account,))]
        snaps = [dict(r) for r in con.execute("SELECT date, MAX(timestamp) AS ts, current_capital FROM portfolio_snapshots WHERE account_id = ? "
                                                "GROUP BY date ORDER BY date", (account,))]
    finally:
        con.close()
    live = {} if account in ("CRYPTO_CALM", "CRYPTO_DIP_REVERSAL") else _live_prices([p["instrument_key"] for p in pos if p.get("instrument_key")])   # crypto: Delta Exchange India prices
    value = {"MCX_TREND_CARRY": _value_mcx, "CRYPTO_CALM": _value_crypto, "CRYPTO_DIP_REVERSAL": _value_crypto}.get(account, _value_position)
    for p in pos:
        value(p, live)
    if acc:
        acc = dict(acc)
        acc["margin_used"] = round(sum(p["margin_used"] for p in pos), 2)
        acc["free_cash"] = round(acc["current_capital"] - acc["margin_used"], 2)
        if account in ("CRYPTO_CALM", "CRYPTO_DIP_REVERSAL"):
            open_pnl = sum(p.get("net_if_sold") or 0 for p in pos)
            acc["business_value"] = round(acc["current_capital"] + open_pnl, 2)
            acc["strict_value"] = round(acc["current_capital"] + open_pnl, 2)
            acc["pretax_value"] = round(acc["current_capital"] + open_pnl, 2)
    return {"available": True, "account_id": account, "accounts": tabs, "account": acc, "positions": pos, "trades": trades, "equity": snaps,
            "stats": {"closed": len(trades), "wins": sum(1 for t in trades if (t["net_pnl"] or 0) > 0), "net": round(sum(t["net_pnl"] or 0 for t in trades)),
                      "fees": round(sum(t["total_friction"] or 0 for t in trades))}}


def work(run: dict) -> None:
    global _running
    try:
        run["status"] = "running"
        save(run)
        p = run["params"]
        def progress(i, n):
            run["progress"] = round(i / n * 100)
        if run["market"] == "crypto":
            from markets.crypto.strategies.tf_5min.dip_reversal import strategy as dr
            try:
                run["result"] = dr.web_backtest(p["capital"], p["start"], p["end"], symbols=p.get("symbols"), leverage=p.get("leverage", 3.0), tax=p.get("tax", "business"),
                                                risk_pct=(p["risk_pct"] / 100 if "risk_pct" in p else None), progress=progress)
            except ValueError as e:
                raise BacktestError(str(e))
        elif run["market"] == "commodity":                        # trend_carry: monthly, all 8 commodities ranked together, its own backtest
            from markets.commodity import futures_eod
            from markets.commodity.strategies.tf_monthly.trend_carry import strategy as tc
            try:
                run["result"] = tc.backtest(futures_eod.load(), p["capital"], p["start"], p["end"], exposure=p["leverage"], symbols=p["symbols"])
            except ValueError as e:
                raise BacktestError(str(e))
        else:
            run["result"] = run_backtest(registry.get(run["market"], run["strategy"]), p["symbols"], p["start"], p["end"], capital=p["capital"], slippage_bp=p["slippage_bp"],
                                         risk_pct=p["risk_pct"], max_positions=int(p["max_positions"]), leverage=p["leverage"], progress=progress,
                                         trail_stop_pct=p["trail_stop_pct"], trail_after_pct=p["trail_after_pct"], profit_trail_pct=p["profit_trail_pct"],
                                         pyramid_pct=p.get("pyramid_pct"),
                                         respect_trades=False,              # trade exactly the symbols ticked on the page
                                         on_snapshot=lambda snap: run.__setitem__("partial", snap))
        try:                                                             # the strategy's own lines / RSI band / results days for the chart
            from engine.chart_overlays import overlays
            if run["market"] != "crypto":                             # calm_coins brings its own volatility band
                run["result"]["overlays"] = overlays(run["strategy"], run["market"], run["result"])
        except Exception:
            traceback.print_exc()
        run["status"] = "done"
    except BacktestError as e:
        run["status"], run["error"] = "failed", str(e)
    except Exception as e:                                           # a bug in someone's strategy must not kill the service
        traceback.print_exc()
        run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"
    run["progress"] = 100
    run.pop("partial", None)
    save(run)
    prune()
    with _lock:
        _running = None


def start(body: dict) -> dict:
    global _running
    spec = validate(body)
    with _lock:
        if _running is not None:
            raise RuntimeError(f"Another backtest is still running ({_running['strategy']}, {_running.get('progress', 0)}% done). Wait for it to finish; the New backtest page shows it live.")
        run = {"id": uuid.uuid4().hex[:12], "status": "queued", "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "progress": 0, **spec,
               "label": str(body.get("label") or "").strip()[:60]}
        _running = run
    save(run)
    threading.Thread(target=work, args=(run,), daemon=True).start()
    return run


def _json_safe(o):
    """JSON has no Infinity / NaN (a browser's JSON.parse rejects the whole response): send them as text. A run with no losing trade has an
    infinite profit factor -- this froze the New backtest page on "Running" (2026-10-15); the page already shows "Infinity" as "no losers"."""
    if isinstance(o, float) and not math.isfinite(o):
        return "Infinity" if o > 0 else "-Infinity" if o < 0 else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


class Handler(BaseHTTPRequestHandler):
    server_version = "hft-backtest"
    def _send(self, status: int, payload) -> None:
        body = json.dumps(_json_safe(payload), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                      # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/strategies":
            return self._send(200, catalog())
        if path == "/api/backtests":
            return self._send(200, summaries())
        if path == "/api/paper":
            try:
                from urllib.parse import parse_qs
                acct = (parse_qs(urlparse(self.path).query).get("account") or ["CRYPTO_DIP_REVERSAL"])[0]
                return self._send(200, paper_summary(acct))
            except Exception as e:                                       # a locked or half-written DB must not break the page
                return self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"paper account unavailable: {type(e).__name__}"})
        m = re.fullmatch(r"/api/backtests/([0-9a-f]{12})", path)
        if m:
            live = _running if _running and _running["id"] == m.group(1) else None
            if live:                                           # running now: progress and the partial result so far (trades, candles, equity)
                return self._send(200, {k: v for k, v in live.items() if k != "partial"} | {"result": live.get("partial")})
            run = load(m.group(1))
            if run:
                return self._send(200, run)
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:                                     # noqa: N802
        m = re.fullmatch(r"/api/backtests/([0-9a-f]{12})/label", urlparse(self.path).path.rstrip("/"))
        if m:
            run = load(m.group(1))
            if not run:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2000)) or b"{}")
            except ValueError:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
            run["label"] = str(body.get("label") or "").strip()[:60]
            save(run)
            return self._send(200, {"id": run["id"], "label": run["label"]})
        if urlparse(self.path).path.rstrip("/") != "/api/backtests":
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        who = self.headers.get("X-Real-IP") or self.client_address[0]
        now = time.time()
        if now - _last_start.get(who, 0) < COOLDOWN_S:
            return self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Please wait a few seconds before starting another backtest"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > 20_000:
                raise ValueError("Request too large")
            run = start(json.loads(self.rfile.read(size) or b"{}"))
        except RuntimeError as e:
            return self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(e)})
        except (ValueError, TypeError) as e:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        _last_start[who] = now
        self._send(HTTPStatus.ACCEPTED, {"id": run["id"], "status": run["status"]})

    def do_DELETE(self) -> None:                                   # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        running = _running["id"] if _running else None
        if path == "/api/backtests":
            gone = 0
            for f in list(RUNS_DIR.glob("*.json")) if RUNS_DIR.exists() else []:
                if f.stem != running:
                    f.unlink(missing_ok=True)
                    gone += 1
            return self._send(200, {"deleted": gone})
        m = re.fullmatch(r"/api/backtests/([0-9a-f]{12})", path)
        if not m:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if m.group(1) == running:
            return self._send(HTTPStatus.CONFLICT, {"error": "That backtest is still running"})
        f = RUNS_DIR / f"{m.group(1)}.json"
        if not f.exists():
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        f.unlink()
        self._send(200, {"deleted": 1})

    do_PUT = do_PATCH = lambda self: self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    def log_message(self, fmt, *args) -> None:                     # quiet
        pass


def fail_orphans() -> None:
    """A run still marked queued/running when the service starts was killed by a restart: mark it failed so nothing shows it as running."""
    for f in RUNS_DIR.glob("*.json") if RUNS_DIR.exists() else []:
        try:
            r = json.loads(f.read_text())
        except ValueError:
            continue
        if r.get("status") in ("queued", "running"):
            r["status"], r["error"] = "failed", "Interrupted by a server restart"
            save(r)


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("BACKTEST_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BACKTEST_PORT", "8000")))
    args = ap.parse_args()
    fail_orphans()

    def _warm_crypto_cache():                   # load_5m_data (12 coins, full history) takes ~20s cold; a backtest's progress stays
        try:                                     # at 0% the whole time -- load it once here so the first real request is already warm
            from markets.crypto.strategies.tf_5min.dip_reversal import strategy as dr
            dr.load_5m_data(list(dr.COINS))
        except Exception:
            traceback.print_exc()
    threading.Thread(target=_warm_crypto_cache, daemon=True).start()

    print(f"backtest api on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
