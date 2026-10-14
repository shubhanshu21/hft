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
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
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
LIMITS = {"capital": (10_000, 10_000_000), "slippage_bp": (0, 100), "risk_pct": (0.1, 10), "max_positions": (1, 10), "leverage": (1, 40),
          "trail_stop_pct": (0, 30), "trail_after_pct": (0, 50), "profit_trail_pct": (0, 20)}
COOLDOWN_S = 5
MAX_SAVED = 200
_lock = threading.Lock()
_running: dict | None = None
_last_start: dict[str, float] = {}


def universe(market: str) -> list[str]:
    return list(NIFTY50_SYMBOLS) if market == "equity" else CURRENCY


def catalog() -> list[dict]:
    return [{"market": m, "name": n, "timeframe": f"{s.timeframe[1]} {s.timeframe[0]}", "supported": tuple(s.timeframe) == ("days", 1), "symbols": list(getattr(s, "universe", None) or universe(m)),
             "summary": (s.__doc__ or "").strip().split("\n")[0], "intraday": s.intraday,
             "default_risk_pct": getattr(s, "default_risk_pct", None)}
            for (m, n), s in sorted(registry.discover().items())]


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
    allowed = known[(market, name)]["symbols"]                     # a strategy may name its own instruments (niftybees_ml: NIFTYBEES)
    symbols = [str(s).upper() for s in body.get("symbols") or allowed]
    if not symbols or any(s not in allowed for s in symbols):
        raise ValueError("Symbols must come from the market's list")
    p = {"symbols": symbols}
    for key, (lo, hi) in LIMITS.items():
        v = float(body.get(key, {"capital": 100_000, "slippage_bp": 5, "risk_pct": 2, "max_positions": 3, "leverage": 1, "trail_stop_pct": 0, "trail_after_pct": 0, "profit_trail_pct": 0}[key]))
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        p[key] = v
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


def paper_summary() -> dict:
    """The paper-trading account, read-only (SQLite opened mode=ro). Open positions are valued at the latest daily close in the archive."""
    import sqlite3
    from core.paths import DB_DIR
    path = DB_DIR / "paper_trading.db"
    if not path.exists():
        return {"available": False}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        acc = con.execute("SELECT * FROM accounts WHERE account_id = 'DRYRUN_ACCOUNT'").fetchone()
        pos = [dict(r) for r in con.execute("SELECT symbol, direction, qty, entry_price, current_stop, best_price, entry_time, strategy FROM positions "
                                              "WHERE account_id = 'DRYRUN_ACCOUNT' AND status = 'OPEN' ORDER BY entry_time")]
        trades = [dict(r) for r in con.execute("SELECT symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, exit_reason, gross_pnl, total_friction, net_pnl, strategy "
                                                 "FROM trades WHERE account_id = 'DRYRUN_ACCOUNT' ORDER BY exit_dt DESC LIMIT 500")]
        snaps = [dict(r) for r in con.execute("SELECT date, MAX(timestamp) AS ts, current_capital FROM portfolio_snapshots WHERE account_id = 'DRYRUN_ACCOUNT' "
                                                "GROUP BY date ORDER BY date")]
    finally:
        con.close()
    for p in pos:
        f = ARCHIVE_ROOT / "equity" / f"{p['symbol']}_daily.csv"
        try:
            last = pd.read_csv(f).iloc[-1]
            p["last_close"], p["last_date"] = float(last["close"]), str(last["timestamp"])[:10]
        except Exception:
            p["last_close"], p["last_date"] = None, None
    return {"available": True, "account": dict(acc) if acc else None, "positions": pos, "trades": trades, "equity": snaps,
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
        run["result"] = run_backtest(registry.get(run["market"], run["strategy"]), p["symbols"], p["start"], p["end"], capital=p["capital"], slippage_bp=p["slippage_bp"],
                                     risk_pct=p["risk_pct"], max_positions=int(p["max_positions"]), leverage=p["leverage"], progress=progress,
                                     trail_stop_pct=p["trail_stop_pct"], trail_after_pct=p["trail_after_pct"], profit_trail_pct=p["profit_trail_pct"],
                                     pyramid_pct=p.get("pyramid_pct"),
                                     on_snapshot=lambda snap: run.__setitem__("partial", snap))
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


class Handler(BaseHTTPRequestHandler):
    server_version = "hft-backtest"
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
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
                return self._send(200, paper_summary())
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
    print(f"backtest api on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
