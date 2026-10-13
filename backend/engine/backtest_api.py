"""Backtest service for the web frontend (the "New backtest" page).  Run:  python3 -m engine.backtest_api   (systemd: hft-backtest-api.service)

    GET  /api/strategies         what can be run: market, name, timeframe, whether the web backtester supports it, symbols
    GET  /api/backtests          saved runs, newest first (summary only)
    GET  /api/backtests/<id>     one run: status, params, and the full replay result when finished
    POST /api/backtests          start a run (JSON body, needs  Authorization: Bearer <BACKTEST_TOKEN>)

It only reads archived candles and runs the strategy code; it never talks to Upstox, touches the paper-trading database or places an order.
Fees (the strategy's cost model) and slippage are always charged. One run at a time; results are saved in var/backtests/<id>.json.
Binds to 127.0.0.1 -- nginx publishes /api/ -- and a run needs the token because it uses the server's CPU.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core import registry
from core.backtester import BacktestError, run_backtest
from core.paths import BACKEND_ROOT, VAR_DIR
from markets.equity.universe import NIFTY50_SYMBOLS

RUNS_DIR = VAR_DIR / "backtests"
CURRENCY = ["USDINR", "EURINR", "GBPINR", "JPYINR"]
LIMITS = {"capital": (10_000, 10_000_000), "slippage_bp": (0, 100), "risk_pct": (0.1, 10), "max_positions": (1, 10), "leverage": (1, 10)}
_lock = threading.Lock()
_running: dict | None = None


def universe(market: str) -> list[str]:
    return list(NIFTY50_SYMBOLS) if market == "equity" else CURRENCY if market == "currency" else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def catalog() -> list[dict]:
    rows = [{"market": m, "name": n, "timeframe": f"{s.timeframe[1]} {s.timeframe[0]}", "supported": tuple(s.timeframe) == ("days", 1), "symbols": universe(m),
             "summary": (s.__doc__ or "").strip().split("\n")[0], "intraday": s.intraday}
            for (m, n), s in sorted(registry.discover().items())]
    rows.append({"market": "crypto", "name": "router", "timeframe": "1 hours", "supported": True, "symbols": universe("crypto"), "intraday": False,
                 "summary": "The live crypto trend router (BTC regime picks long spot or short perpetuals). Runs with its own cost and funding model."})
    return rows


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
        out.append({k: r.get(k) for k in ("id", "status", "created", "market", "strategy", "params", "error")} | {"metrics": (r.get("result") or {}).get("metrics")})
    return sorted(out, key=lambda r: r["created"] or "", reverse=True)


def validate(body: dict) -> dict:
    market, name = str(body.get("market", "")), str(body.get("strategy", ""))
    known = {(r["market"], r["name"]): r for r in catalog()}
    if (market, name) not in known:
        raise ValueError("Unknown market or strategy")
    if not known[(market, name)]["supported"]:
        raise ValueError(f"{market}/{name} uses {known[(market, name)]['timeframe']} bars; the web backtester runs daily-bar strategies")
    allowed = universe(market)
    symbols = [str(s).upper() for s in body.get("symbols") or allowed]
    if not symbols or any(s not in allowed for s in symbols):
        raise ValueError("Symbols must come from the market's list")
    p = {"symbols": symbols}
    for key, (lo, hi) in LIMITS.items():
        v = float(body.get(key, {"capital": 100_000, "slippage_bp": 5, "risk_pct": 2, "max_positions": 3, "leverage": 1}[key]))
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        p[key] = v
    for key in ("start", "end"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key, ""))):
            raise ValueError("Dates must look like 2024-01-31")
        p[key] = body[key]
    return {"market": market, "strategy": name, "params": p}


def crypto_result(params: dict) -> dict:
    """The crypto router as a replay: daily equity from the hourly returns of the live router backtest (its own costs and funding included)."""
    from markets.crypto.backtest import run
    r = run(1.0)["port"].loc[params["start"]: params["end"]]
    eq = (1 + r).cumprod() * params["capital"]
    daily = eq.resample("1D").last().dropna()
    from core.backtester import metrics
    m = metrics([round(v) for v in daily], params["capital"], [], len(daily), 0)
    m["trading_days"] = len(daily)
    return {"days": [d.strftime("%Y-%m-%d") for d in daily.index], "equity": [round(v) for v in daily], "events": [[] for _ in daily], "pos": [[] for _ in daily],
            "prices": {}, "metrics": m | {"note": "Continuous holding: no discrete trades. Costs and funding are inside the return."}, "skipped_symbols": []}


def work(run: dict) -> None:
    global _running
    try:
        run["status"] = "running"
        save(run)
        p = run["params"]
        if run["market"] == "crypto":
            run["result"] = crypto_result(p)
        else:
            def progress(i, n):
                run["progress"] = round(i / n * 100)
            run["result"] = run_backtest(registry.get(run["market"], run["strategy"]), p["symbols"], p["start"], p["end"], capital=p["capital"], slippage_bp=p["slippage_bp"],
                                         risk_pct=p["risk_pct"], max_positions=int(p["max_positions"]), leverage=p["leverage"], progress=progress)
        run["status"] = "done"
    except BacktestError as e:
        run["status"], run["error"] = "failed", str(e)
    except Exception as e:                                           # a bug in someone's strategy must not kill the service
        traceback.print_exc()
        run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"
    run["progress"] = 100
    save(run)
    with _lock:
        _running = None


def start(body: dict) -> dict:
    global _running
    spec = validate(body)
    with _lock:
        if _running is not None:
            raise RuntimeError("Another backtest is still running; try again in a minute")
        run = {"id": uuid.uuid4().hex[:12], "status": "queued", "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "progress": 0, **spec}
        _running = run
    save(run)
    threading.Thread(target=work, args=(run,), daemon=True).start()
    return run


class Handler(BaseHTTPRequestHandler):
    server_version = "hft-backtest"
    token: str | None = None

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
        m = re.fullmatch(r"/api/backtests/([0-9a-f]{12})", path)
        if m:
            run = load(m.group(1)) or (_running if _running and _running["id"] == m.group(1) else None)
            if run:
                live = _running if _running and _running["id"] == run["id"] else None
                return self._send(200, {**run, "progress": live["progress"] if live else run.get("progress", 100)})
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:                                     # noqa: N802
        if urlparse(self.path).path.rstrip("/") != "/api/backtests":
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not self.token or not secrets.compare_digest(given, self.token):
            return self._send(HTTPStatus.UNAUTHORIZED, {"error": "A valid access token is needed to run a backtest"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > 20_000:
                raise ValueError("Request too large")
            run = start(json.loads(self.rfile.read(size) or b"{}"))
        except RuntimeError as e:
            return self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(e)})
        except (ValueError, TypeError) as e:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        self._send(HTTPStatus.ACCEPTED, {"id": run["id"], "status": run["status"]})

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    def log_message(self, fmt, *args) -> None:                     # quiet
        pass


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("BACKTEST_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BACKTEST_PORT", "8000")))
    args = ap.parse_args()
    token = (os.environ.get("BACKTEST_TOKEN") or "").strip()
    Handler.token = token or None
    print(f"backtest api on http://{args.host}:{args.port}  (running a backtest {'needs the token' if token else 'is DISABLED: set BACKTEST_TOKEN in .env'})", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
