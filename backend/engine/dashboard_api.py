"""Read-only statistics API for the Angular dashboard (dashboard/), plus the static hosting of its build.

    python3 -m engine.dashboard_api [--host 127.0.0.1] [--port 5000]

Everything is READ-ONLY: the SQLite database is opened with mode=ro, there is no endpoint that changes anything (no start/stop, no orders), and nothing
here talks to Upstox (so it can never invalidate the daemon's single active token). It binds to 127.0.0.1 by default -- account P&L should not be on the
public internet -- and DASHBOARD_TOKEN, if set, is required on every /api call (Authorization: Bearer <token> or ?token=).

Endpoints (JSON):  /api/overview  /api/equity  /api/daily  /api/symbols  /api/exits  /api/trades?limit=  /api/system
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.paths import BACKEND_ROOT, DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = DB_DIR / "paper_trading.db"
STATIC_DIR = BACKEND_ROOT.parent / "dashboard" / "dist" / "dashboard" / "browser"
ACCOUNT = "DRYRUN_ACCOUNT"


# ---------------------------------------------------------------------------------------------------------------- data access
def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def load_trades(con, account: str = ACCOUNT) -> list[dict]:
    return _rows(con, "SELECT * FROM trades WHERE account_id = ? ORDER BY exit_dt, trade_id", (account,))


def load_account(con, account: str = ACCOUNT) -> dict:
    rows = _rows(con, "SELECT * FROM accounts WHERE account_id = ?", (account,))
    return rows[0] if rows else {}


# ---------------------------------------------------------------------------------------------------------------- statistics
def trade_stats(trades: list[dict]) -> dict:
    """Headline numbers for a list of closed trades (net of every cost)."""
    pnl = [float(t["net_pnl"]) for t in trades]
    n = len(pnl)
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    gross_w, gross_l = sum(wins), -sum(losses)
    gross_pnl = sum(float(t["gross_pnl"]) for t in trades)
    friction = sum(float(t["total_friction"]) for t in trades)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "need_pct": round(100.0 * avg_loss / (avg_win + avg_loss), 1) if (avg_win + avg_loss) else 0.0,   # win rate needed to break even at this payoff
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "expectancy": round(sum(pnl) / n, 2) if n else 0.0,
        "net": round(sum(pnl), 2), "gross": round(gross_pnl, 2), "costs": round(friction, 2),
        "costs_pct_of_gross": round(100.0 * friction / gross_pnl, 1) if gross_pnl > 0 else None,
        "best": round(max(pnl), 2) if pnl else 0.0, "worst": round(min(pnl), 2) if pnl else 0.0,
        "avg_hold_min": round(sum(float(t.get("hold_minutes") or 0) for t in trades) / n, 1) if n else 0.0,
    }


def equity_curve(trades: list[dict], initial: float) -> list[dict]:
    """One point per closed trade (plus the starting balance), each with its drawdown from the running peak."""
    pts, peak = [{"t": None, "equity": round(initial, 2), "drawdown_pct": 0.0}], initial
    for t in trades:
        eq = float(t["capital_after"])
        peak = max(peak, eq)
        pts.append({"t": t["exit_dt"], "equity": round(eq, 2), "drawdown_pct": round((peak - eq) / peak * 100, 2) if peak else 0.0})
    return pts


def max_drawdown_pct(points: list[dict]) -> float:
    return max((p["drawdown_pct"] for p in points), default=0.0)


def daily_pnl(trades: list[dict]) -> list[dict]:
    days: dict[str, dict] = {}
    for t in trades:
        d = str(t["exit_dt"])[:10]
        row = days.setdefault(d, {"date": d, "net": 0.0, "trades": 0, "wins": 0})
        row["net"] += float(t["net_pnl"])
        row["trades"] += 1
        row["wins"] += 1 if float(t["net_pnl"]) > 0 else 0
    return [{**r, "net": round(r["net"], 2)} for r in sorted(days.values(), key=lambda r: r["date"])]


def by_symbol(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(t["symbol"], []).append(t)
    out = []
    for sym, g in groups.items():
        cum, series = 0.0, []
        for t in g:
            cum += float(t["net_pnl"])
            series.append(round(cum, 2))
        out.append({"symbol": sym, **trade_stats(g), "series": series[-60:]})
    return sorted(out, key=lambda r: -r["net"])


def by_exit_reason(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        groups.setdefault(t["exit_reason"], []).append(float(t["net_pnl"]))
    return sorted(({"reason": k, "trades": len(v), "net": round(sum(v), 2), "avg": round(sum(v) / len(v), 2)} for k, v in groups.items()),
                  key=lambda r: -r["trades"])


def open_positions(con, account: str = ACCOUNT) -> list[dict]:
    rows = _rows(con, "SELECT symbol, direction, qty, entry_price, current_stop, target_price, entry_time, strategy FROM positions "
                      "WHERE account_id = ? AND status = 'OPEN' ORDER BY entry_time", (account,))
    return [{**r, "entry_price": round(r["entry_price"], 4), "current_stop": round(r["current_stop"], 4),
             "target_price": round(r["target_price"], 4) if r["target_price"] else None} for r in rows]


def overview(con, account: str = ACCOUNT, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    acct, trades = load_account(con, account), load_trades(con, account)
    initial = float(acct.get("initial_capital") or 0.0)
    current = float(acct.get("current_capital") or initial)
    today = now.strftime("%Y-%m-%d")
    todays = [t for t in trades if str(t["exit_dt"])[:10] == today]
    curve = equity_curve(trades, initial)
    stats = trade_stats(trades)
    stats["max_drawdown_pct"] = max_drawdown_pct(curve)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "account": {"initial": round(initial, 2), "current": round(current, 2), "net": round(current - initial, 2),
                    "return_pct": round((current / initial - 1) * 100, 2) if initial else 0.0, "since": str(acct.get("created_at") or "")[:10]},
        "today": {"date": today, **{k: v for k, v in trade_stats(todays).items() if k in ("trades", "wins", "net", "win_pct")}},
        "kpis": stats,
        "open_positions": open_positions(con, account),
    }


def recent_trades(con, limit: int = 50, account: str = ACCOUNT) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    rows = _rows(con, "SELECT trade_id, symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, hold_minutes, exit_reason, gross_pnl, "
                      "total_friction, net_pnl, strategy FROM trades WHERE account_id = ? ORDER BY trade_id DESC LIMIT ?", (account, limit))
    return rows


# ---------------------------------------------------------------------------------------------------------------- system panel
def system_info(now: datetime | None = None) -> dict:
    """Daemon health and the Upstox-derived limits in force. Reads local files only; never calls Upstox."""
    now = now or datetime.now(IST)
    out: dict = {"generated_at": now.isoformat(timespec="seconds")}
    try:
        from engine import heartbeat
        hb = heartbeat.read()
        out["heartbeat"] = {"phase": hb.get("phase"), "ts": hb.get("ts"), "problem": heartbeat.stale_reason(now, hb)} if hb else None
    except Exception:
        out["heartbeat"] = None
    try:
        from engine.config import UpstoxConfig
        from services.auth.upstox_auto_login import token_expiry_epoch
        exp = token_expiry_epoch(UpstoxConfig.cached_token())
        out["token_hours_left"] = round((exp - now.timestamp()) / 3600, 1) if exp else None
    except Exception:
        out["token_hours_left"] = None
    try:
        from engine.backup_db import list_backups
        b = list_backups()
        out["last_backup"] = datetime.fromtimestamp(b[0].stat().st_mtime, IST).isoformat(timespec="minutes") if b else None
    except Exception:
        out["last_backup"] = None
    try:
        control = json.loads((DB_DIR / f".{ACCOUNT}_control.json").read_text())
    except (OSError, ValueError):
        control = {}
    out["trading_enabled"] = control.get("enabled", True)
    # per-symbol Upstox limits from the daemon's cache (engine/margin_rates.py) and the sizing settings in force
    try:
        from engine import margin_rates
        symbols = os.environ.get("DRYRUN_SYMBOLS", "").split()
        rates = margin_rates._load()
        rows = []
        for s in symbols:
            r = rates.get(s.upper())
            if r:
                rows.append({"symbol": s, "margin_per_lot": round(r["margin"]), "upstox_leverage": round(r["leverage"], 2),
                             "lot_size": r.get("lot_size"), "tick_size": r.get("tick_rs"), "as_of": r.get("asof")})
        out["upstox_limits"] = rows
    except Exception:
        out["upstox_limits"] = []
    try:
        from core import sessions
        out["sessions"] = [{"market": m, "open": f"{sessions.window(m)[0] // 60:02d}:{sessions.window(m)[0] % 60:02d}",
                            "close": f"{sessions.window(m)[1] // 60:02d}:{sessions.window(m)[1] % 60:02d}",
                            "last_entry": f"{sessions.last_entry_min(m) // 60:02d}:{sessions.last_entry_min(m) % 60:02d}",
                            "forced_exit": f"{sessions.squareoff_min(m) // 60:02d}:{sessions.squareoff_min(m) % 60:02d}"}
                           for m in ("commodity", "currency", "equity")]
    except Exception:
        out["sessions"] = []
    return out


# ---------------------------------------------------------------------------------------------------------------- HTTP
def route(path: str, query: dict, db_path: Path = DB_PATH) -> tuple[int, object]:
    """Pure routing for /api/*, so it can be tested without a socket. Returns (status, json-able)."""
    if path == "/api/system":
        return 200, system_info()
    if not db_path.exists():
        return 503, {"error": "database not found"}
    con = connect(db_path)
    try:
        if path == "/api/overview":
            return 200, overview(con)
        if path == "/api/equity":
            acct = load_account(con)
            return 200, equity_curve(load_trades(con), float(acct.get("initial_capital") or 0.0))
        if path == "/api/daily":
            return 200, daily_pnl(load_trades(con))
        if path == "/api/symbols":
            return 200, by_symbol(load_trades(con))
        if path == "/api/exits":
            return 200, by_exit_reason(load_trades(con))
        if path == "/api/trades":
            return 200, recent_trades(con, int((query.get("limit") or ["50"])[0]))
    except (sqlite3.Error, ValueError) as exc:
        return 500, {"error": str(exc)}
    finally:
        con.close()
    return 404, {"error": "not found"}


def safe_static_path(root: Path, url_path: str) -> Path | None:
    """The file under `root` for a URL path, or None if it would escape `root` (directory traversal) or is not a file."""
    rel = url_path.lstrip("/") or "index.html"
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def authorized(headers, query: dict, token: str | None) -> bool:
    if not token:
        return True
    given = (headers.get("Authorization") or "").removeprefix("Bearer ").strip() or (query.get("token") or [""])[0]
    return given == token


class Handler(BaseHTTPRequestHandler):
    server_version = "hft-dashboard"
    static_dir: Path = STATIC_DIR
    token: str | None = None
    db_path: Path = DB_PATH

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "public, max-age=300")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                      # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path.startswith("/api/"):
            if not authorized(self.headers, query, self.token):
                return self._send(HTTPStatus.UNAUTHORIZED, b'{"error":"unauthorized"}', "application/json")
            status, payload = route(url.path, query, self.db_path)
            return self._send(status, json.dumps(payload, default=str).encode(), "application/json")
        f = safe_static_path(self.static_dir, url.path)
        if f is None and "." not in Path(url.path).name:            # SPA route (no file extension) -> index.html
            f = safe_static_path(self.static_dir, "/index.html")
        if f is None:
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self) -> None:                                     # noqa: N802
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"read-only"}', "application/json", {"Allow": "GET"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args) -> None:                    # quiet: the daemon's own logs are the record
        pass


def make_server(host: str, port: int, token: str | None = None, db_path: Path = DB_PATH, static_dir: Path = STATIC_DIR) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"token": token, "db_path": db_path, "static_dir": static_dir})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "5000")))
    args = ap.parse_args(argv)
    token = os.environ.get("DASHBOARD_TOKEN") or None
    server = make_server(args.host, args.port, token)
    exposed = args.host not in ("127.0.0.1", "localhost", "::1")
    print(f"dashboard on http://{args.host}:{args.port}  (read-only, token {'required' if token else 'NOT set'}){'  WARNING: reachable from the network' if exposed else ''}",
          flush=True)
    if exposed and not token:
        print("WARNING: bound to a network address without DASHBOARD_TOKEN -- anyone who can reach this port can read the account's P&L.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
