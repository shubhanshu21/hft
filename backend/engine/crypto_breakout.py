"""The crypto ACTIVE sleeve: daily volatility breakout, long-only, on USDT-M perpetuals of Binance's DEMO exchange.  (docs/CRYPTO_ACTIVE_STRATEGIES.md)

    python3 -m engine.crypto_breakout --status
    python3 -m engine.crypto_breakout --once        # one cycle (the daemon runs it from engine/crypto_paper.py's loop)
    python3 -m engine.crypto_breakout --reset [--capital 500]

Rule: markets/crypto/strategies/tf_15min/breakout.py (the same function the backtest uses). Each coin gets an equal share of this sleeve's equity at 1x; a coin that
breaks out goes long until the UTC day ends. The sleeve runs INSIDE the crypto daemon, right after the slow blend's cycle, on the same demo account:
the exchange holds ONE perpetual position per coin, so the blend subtracts this sleeve's open quantity and funding from what it reads back
(open_qty / funding_total) -- running in one process, in turn, keeps the two books consistent without locks.
Its own ledger: var/db/crypto_breakout.db. Settings (backend/.env): CRYPTO_BREAKOUT_ENABLED, CRYPTO_BREAKOUT_CAPITAL_USDT, CRYPTO_SYMBOLS.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.paths import DB_DIR
from markets.crypto.strategies.tf_15min import breakout
from services.broker.binance_demo import floor_step
from services.data import binance
from services.utils import telegram

log = logging.getLogger("crypto_breakout")
DB_PATH = DB_DIR / "crypto_breakout.db"
SIM_COST = 0.0006                      # paper fallback: taker 0.05% + ~1 bp slippage per side (the backtest's assumption)
HISTORY_DAYS = breakout.TREND_DAYS + 10
PERP_LEVERAGE = 2                      # isolated-margin setting on the demo account, shared with the blend's perpetual legs

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, qty REAL NOT NULL, entry_price REAL NOT NULL, entry_ts TEXT NOT NULL, funding REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_ts TEXT, exit_ts TEXT, qty REAL, entry_price REAL, exit_price REAL,
                                   fees REAL, funding REAL, pnl REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value) -> None:
    con.execute("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def reset(con, capital: float) -> None:
    for t in ("state", "positions", "trades", "equity"):
        con.execute(f"DELETE FROM {t}")
    set_state(con, "capital", capital)
    set_state(con, "realized", 0.0)
    set_state(con, "funding_total", 0.0)
    con.commit()


# -- read by the blend (engine/crypto_paper.py) so the shared demo position is split correctly ---------------------------------------------------------------
def open_qty(symbol: str, path: Path | None = None) -> float:
    path = path or DB_PATH
    if not path.exists():
        return 0.0
    with closing(sqlite3.connect(path)) as con:
        try:
            row = con.execute("SELECT qty FROM positions WHERE symbol=?", (symbol,)).fetchone()
        except sqlite3.OperationalError:
            return 0.0
    return float(row[0]) if row else 0.0


def funding_total(path: Path | None = None) -> float:
    """Funding this sleeve has paid (-) / received (+) in total, as a running sum the blend can difference."""
    path = path or DB_PATH
    if not path.exists():
        return 0.0
    with closing(sqlite3.connect(path)) as con:
        try:
            return float(get_state(con, "funding_total", 0.0))
        except sqlite3.OperationalError:
            return 0.0


# -- market data ---------------------------------------------------------------------------------------------------------------------------------------------
def closed_bars(symbol: str, now: datetime) -> pd.DataFrame:
    binance.topup(symbol, "15m")                                    # appends CLOSED candles only
    df = pd.read_csv(binance.archive_path(symbol, "15m"), parse_dates=["timestamp"]).drop_duplicates("timestamp").set_index("timestamp").sort_index()
    df = df[df.index + breakout.BAR <= pd.Timestamp(now)]
    return df.loc[df.index[-1] - pd.Timedelta(days=HISTORY_DAYS):] if len(df) else df


# -- execution ------------------------------------------------------------------------------------------------------------------------------------------------
class SimFills:
    name = "paper"

    def market(self, symbol: str, side: str, qty: float, price: float) -> dict:
        return {"qty": qty, "avg_price": price, "fee": qty * price * SIM_COST}


class DemoFills:
    """Market orders on Binance's DEMO futures exchange (services/broker/binance_demo.py can only reach the demo hosts). No reduce-only: the
    blend may hold the opposite side of the same coin, and the exchange nets the two."""
    name = "binance-demo"

    def __init__(self, client):
        self.c = client
        self._margin_set: set[str] = set()

    def market(self, symbol: str, side: str, qty: float, price: float) -> dict | None:
        f = self.c.filters("futures", symbol)
        q = floor_step(qty, f["step"])
        if q < f["min_qty"] or (side == "BUY" and q * price < f["min_notional"]):
            log.info("%s: breakout order of %.6g is below the exchange minimum (%.6g / %.2f USDT) -- skipped", symbol, q, f["min_qty"], f["min_notional"])
            return None
        if symbol not in self._margin_set:
            self.c.set_isolated(symbol, PERP_LEVERAGE)
            self._margin_set.add(symbol)
        r = self.c.futures_market(symbol, side, q)
        return {"qty": r["qty"], "avg_price": r["avg_price"] or price, "fee": r["fee"]}


def make_fills(mode: str | None = None):
    mode = (mode or os.environ.get("CRYPTO_EXECUTION", "auto")).lower()
    key, secret = os.environ.get("BINANCE_DEMO_API_KEY", "").strip(), os.environ.get("BINANCE_DEMO_API_SECRET", "").strip()
    if mode == "demo" or (mode == "auto" and key and secret):
        from services.broker.binance_demo import BinanceDemo
        return DemoFills(BinanceDemo(key, secret))
    return SimFills()


# -- one cycle -----------------------------------------------------------------------------------------------------------------------------------------------
def equity(con, prices: dict[str, float]) -> float:
    cap = float(get_state(con, "capital", 0.0)) + float(get_state(con, "realized", 0.0))
    for s, q, e, _, fund in con.execute("SELECT symbol, qty, entry_price, entry_ts, funding FROM positions"):
        if s in prices:
            cap += q * (prices[s] - e) + fund
    return cap


def run_cycle(con, symbols, fills, now: datetime | None = None, get_bars=closed_bars, get_funding=None, alert=telegram.send) -> list[dict]:
    """Decide on the latest closed 15-minute bar of each coin and trade the difference. Returns what was done."""
    now = now or datetime.now(timezone.utc)
    if get_funding is None:
        from engine.crypto_paper import funding_events as get_funding
    bars = {s: get_bars(s, now) for s in symbols}
    prices = {s: float(b["close"].iloc[-1]) for s, b in bars.items() if len(b)}
    now_ms = int(now.timestamp() * 1000)
    last_ms = int(float(get_state(con, "last_funding_ms", now_ms)))
    # funding on open positions since the last cycle (a long pays a positive rate)
    for s, q, e, ets, fund in list(con.execute("SELECT symbol, qty, entry_price, entry_ts, funding FROM positions")):
        if s in prices and q:
            paid = sum(-q * prices[s] * r for r in get_funding(s, last_ms + 1, now_ms))
            if paid:
                con.execute("UPDATE positions SET funding = funding + ? WHERE symbol = ?", (paid, s))
                set_state(con, "funding_total", float(get_state(con, "funding_total", 0.0)) + paid)
    set_state(con, "last_funding_ms", now_ms)
    eq = equity(con, prices)
    share = eq / max(len(symbols), 1)
    done = []
    for s in symbols:
        if s not in prices:
            continue
        want = breakout.target(bars[s])
        row = con.execute("SELECT qty, entry_price, entry_ts, funding FROM positions WHERE symbol=?", (s,)).fetchone()
        held = row[0] if row else 0.0
        px = prices[s]
        if want and not held:
            r = fills.market(s, "BUY", share / px, px)
            if r and r["qty"] > 0:
                con.execute("INSERT OR REPLACE INTO positions(symbol, qty, entry_price, entry_ts, funding) VALUES(?,?,?,?,?)",
                            (s, r["qty"], r["avg_price"], now.isoformat(timespec="seconds"), -r["fee"]))
                done.append({"symbol": s, "side": "BUY", "qty": r["qty"], "price": r["avg_price"]})
        elif held and not want:
            r = fills.market(s, "SELL", held, px)
            if r and r["qty"] > 0:
                q, e, ets, fund = row
                pnl = r["qty"] * (r["avg_price"] - e) + fund - r["fee"]          # fund already carries the entry fee and the funding paid
                con.execute("INSERT INTO trades(symbol, entry_ts, exit_ts, qty, entry_price, exit_price, fees, funding, pnl, reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (s, ets, now.isoformat(timespec="seconds"), r["qty"], e, r["avg_price"], r["fee"], fund, pnl, "day_end"))
                set_state(con, "realized", float(get_state(con, "realized", 0.0)) + pnl)
                left = q - r["qty"]
                if abs(left) * px < 1.0:
                    con.execute("DELETE FROM positions WHERE symbol=?", (s,))
                else:                                                          # a partial fill: keep the rest open, its fee/funding already booked
                    con.execute("UPDATE positions SET qty=?, funding=0 WHERE symbol=?", (left, s))
                done.append({"symbol": s, "side": "SELL", "qty": r["qty"], "price": r["avg_price"], "pnl": pnl})
    eq = equity(con, prices)
    con.execute("INSERT OR REPLACE INTO equity(ts, equity) VALUES(?, ?)", (now.isoformat(timespec="seconds"), eq))
    con.commit()
    if done:
        msg = "; ".join(f"{d['side']} {d['qty']:.5g} {d['symbol']} @ {d['price']:,.2f}" + (f" (P&L {d['pnl']:+,.2f})" if "pnl" in d else "") for d in done)
        log.info("breakout: %s | sleeve equity %.2f", msg, eq)
        alert(f"<b>CRYPTO breakout {fills.name}</b> {msg}\nSleeve equity {eq:,.2f} USDT")
    return done


def status(con) -> str:
    cap = float(get_state(con, "capital", 0.0) or 0.0)
    if not cap:
        return "No crypto breakout sleeve yet."
    last = con.execute("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    lines = [f"CRYPTO BREAKOUT sleeve (daily volatility breakout k={breakout.K}, trend {breakout.TREND_DAYS}d, long-only, 1x) -- started with {cap:,.0f} USDT"]
    if last:
        lines.append(f"  {last[0]}: equity {last[1]:,.2f} USDT ({(last[1] / cap - 1) * 100:+.2f}%), realized {float(get_state(con, 'realized', 0)):+,.2f}, funding {float(get_state(con, 'funding_total', 0)):+,.2f}")
    for s, q, e, ets, _ in con.execute("SELECT symbol, qty, entry_price, entry_ts, funding FROM positions"):
        lines.append(f"  OPEN long {s}: {q:.6g} @ {e:,.2f} since {ets}")
    n, wins = con.execute("SELECT COUNT(*), SUM(pnl > 0) FROM trades").fetchone()
    lines.append(f"  closed trades: {n or 0} ({wins or 0} winners)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--capital", type=float, default=None)
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    load_dotenv(BACKEND_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    symbols = tuple(os.environ.get("CRYPTO_SYMBOLS", "BTCUSDT ETHUSDT SOLUSDT").split())
    with closing(connect()) as con:
        if a.status:
            print(status(con)); return 0
        if a.reset or get_state(con, "capital") is None:
            reset(con, a.capital or float(os.environ.get("CRYPTO_BREAKOUT_CAPITAL_USDT", os.environ.get("CRYPTO_CAPITAL_USDT", "500"))))
            if a.reset:
                print(status(con)); return 0
        if a.once:
            run_cycle(con, symbols, make_fills()); print(status(con))
    return 0


if __name__ == "__main__":
    sys.exit(main())
