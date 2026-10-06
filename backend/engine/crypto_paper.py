"""Paper trading for BTC / ETH / SOL on Binance public data: a REGIME ROUTER that goes long in bull and sideways markets and short in bear markets.  (docs/CRYPTO_MOMENTUM.md, docs/CRYPTO_ALL_WEATHER.md)

    python3 -m engine.crypto_paper                 # the daemon (systemd: hft-crypto.service)
    python3 -m engine.crypto_paper --once          # one cycle, then exit
    python3 -m engine.crypto_paper --status        # holdings, equity, buy-and-hold comparison
    python3 -m engine.crypto_paper --reset [--capital 10000]

EXECUTION (CRYPTO_EXECUTION): "demo" sends real market orders to BINANCE'S DEMO EXCHANGE (spot longs on demo-api.binance.com, perpetual shorts / leveraged excess on demo-fapi.binance.com; services/broker/binance_demo.py, which can
only talk to those two hosts) with keys BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET created at demo.binance.com; "paper" = our own simulation of the fills; "auto" (default) = demo when the keys are set, else paper.
Either way nothing here can reach the live exchange. Market data comes from Binance's PUBLIC klines. State (sizing ledger, trades, equity) lives in var/db/crypto_paper.db, separate from the Upstox paper account.
CRYPTO_LEVERAGE (default 1.0) multiplies every target: spot holds a long up to 1x the coin's share, a short or the part of a long above 1x is a USDT-M perpetual (isolated margin).

Strategy (CRYPTO_STRATEGY: "blend" (default) = half the capital on the router and half on the trend ensemble (CRYPTO_ROUTER_WEIGHT, default 0.5); "router" = the regime router alone; "trend" = the long/flat spot ensemble alone): markets/crypto/router.py. BTC's regime (price vs the 200-day EMA and the 50/200-day EMA order) picks the book --
bull / sideways: long spot, sized by the slow-momentum ensemble (EMA 20/50d + Donchian 20/10d + 90-day momentum, volatility-targeted); bear: short perpetuals, larger the fewer trend signals are on, for coins that are themselves in a bear regime.
Signals use CLOSED HOURLY bars. The loop wakes every 15 minutes: it tops up the hourly archive, and when a new hourly bar has closed it decides each coin's signed target size and rebalances at the latest 15-minute price with the backtest's
costs (spot 0.12% per side, SOL 0.15%; perp shorts 0.09%). A short earns the perpetual's funding rate (received when positive) every 8 hours. Between decisions it only records equity.
Each coin gets an equal third of the equity; a trade happens only when the target differs from the holding by more than REBALANCE_BAND of that share (default 10%), or the target is zero and a position is held.
Settings (backend/.env): CRYPTO_SYMBOLS, CRYPTO_CAPITAL_USDT (start of a NEW account), CRYPTO_REBALANCE_BAND, CRYPTO_STRATEGY, CRYPTO_LEVERAGE, CRYPTO_EXECUTION, ENABLE_CRYPTO_TRADING.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal as os_signal
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from core.paths import DB_DIR
from markets.crypto import router
from services.broker.binance_demo import floor_step
from markets.crypto.momentum import BARS_PER_DAY, COST_SIDE, WARMUP_DAYS, ensemble_position
from services.data import binance
from services.utils import telegram

log = logging.getLogger("crypto_paper")
DB_PATH = DB_DIR / "crypto_paper.db"
LOCK_PATH = DB_DIR / ".crypto_paper.lock"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
MIN_NOTIONAL = 10.0
PERP_SIDE = 0.0009                  # perpetual futures: taker fee 0.05% + slippage, per side (a short's cost)
CYCLE_SECONDS = 900                 # 15 minutes


# -- storage ---------------------------------------------------------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS holdings (symbol TEXT PRIMARY KEY, qty REAL NOT NULL, avg_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, notional REAL, cost REAL, target REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, cash REAL, buy_hold REAL, holdings_json TEXT);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def get_state(con, key: str, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key: str, value) -> None:
    con.execute("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def holdings(con) -> dict[str, float]:
    return {s: q for s, q, _ in con.execute("SELECT symbol, qty, avg_price FROM holdings")}


def reset(con, capital: float, symbols) -> None:
    for t in ("state", "holdings", "trades", "equity"):
        con.execute(f"DELETE FROM {t}")
    set_state(con, "capital", capital)
    set_state(con, "cash", capital)
    set_state(con, "symbols", " ".join(symbols))
    con.commit()


# -- pure logic (unit-tested) ------------------------------------------------------------------------------------------------------------------------------
def plan_trades(equity: float, prices: dict[str, float], qty: dict[str, float], targets: dict[str, float], band: float = 0.10, min_notional: float = MIN_NOTIONAL, max_abs: float = 1.0) -> list[dict]:
    """The rebalance that brings each coin to its SIGNED target share. Every coin gets equity/len(coins); target[s] in [-max_abs, max_abs] is the fraction of that share to hold (positive = long spot, negative = short perpetual)."""
    share = equity / max(len(prices), 1)
    orders = []
    for s, price in prices.items():
        want = share * max(-max_abs, min(max_abs, targets.get(s, 0.0)))
        have = qty.get(s, 0.0) * price
        diff = want - have
        flat_exit = want == 0.0 and abs(have) > min_notional
        if not flat_exit and abs(diff) < max(band * share, min_notional):
            continue
        if flat_exit:
            orders.append({"symbol": s, "side": "SELL" if have > 0 else "BUY", "qty": abs(qty[s]), "reason": "exit (flat signal)"})
        elif diff > 0:
            orders.append({"symbol": s, "side": "BUY", "qty": diff / price, "reason": "cover / flip to long" if have < 0 else ("target up" if have > 0 else "entry")})
        else:
            orders.append({"symbol": s, "side": "SELL", "qty": -diff / price, "reason": "flip to short" if want < 0 and have >= 0 else ("target down" if have > 0 else "add short" if have < 0 else "entry short")})
    return orders


def apply_orders(cash: float, qty: dict[str, float], avg: dict[str, float], prices: dict[str, float], orders: list[dict], cost_side: dict[str, float], perp_side: float = PERP_SIDE, allow_borrow: bool = False) -> tuple[float, list[dict]]:
    """Fill the orders at `prices`. Cost (fee + slippage) is charged on the notional of each leg: the part that changes a LONG (spot) pays the coin's spot cost, the part that changes a SHORT (perpetual) pays `perp_side`.
    SELLs first, so cash is available for the BUYs; a BUY is trimmed to the cash that is left. Shorts add their proceeds to cash (equity = cash + sum(qty x price))."""
    fills = []
    for o in sorted(orders, key=lambda o: o["side"] != "SELL"):
        s, px, c = o["symbol"], prices[o["symbol"]], cost_side.get(o["symbol"], 0.0015)
        prior = qty.get(s, 0.0)
        q = o["qty"]
        if o["side"] == "BUY" and not allow_borrow:                            # (leveraged: the margin comes from the perpetual account, so cash may go negative)
            q = min(q, cash / (px * (1 + max(c, perp_side))))
        if q * px < MIN_NOTIONAL and o["reason"] != "exit (flat signal)":
            continue
        new = prior + q if o["side"] == "BUY" else prior - q
        if abs(new) < 1e-12:
            new = 0.0
        spot_traded = abs(max(new, 0.0) - max(prior, 0.0)) * px
        perp_traded = abs(max(-new, 0.0) - max(-prior, 0.0)) * px
        cost = spot_traded * c + perp_traded * perp_side
        cash += (-q if o["side"] == "BUY" else q) * px - cost
        if new != 0.0 and (prior == 0.0 or (prior > 0) != (new > 0)):
            avg[s] = px                                                       # a new position (or a flip): the entry price
        elif abs(new) > abs(prior):
            avg[s] = (avg.get(s, px) * abs(prior) + px * abs(new - prior)) / abs(new)
        qty[s] = new
        fills.append({**o, "qty": q, "price": px, "notional": q * px, "cost": cost})
    return cash, fills


def accrue_funding(con: sqlite3.Connection, symbols, qty: dict[str, float], prices: dict[str, float], now: datetime, get_funding=None, share: float | None = None) -> float:
    """Funding on the perpetual SHORTS since the last time it was applied: a short receives rate x notional every 8 hours when the rate is positive (pays when negative). Returns the cash change."""
    get_funding = get_funding or funding_events
    last = get_state(con, "last_funding_ms")
    now_ms = int(now.timestamp() * 1000)
    if last is None:
        set_state(con, "last_funding_ms", now_ms)
        return 0.0
    total = 0.0
    for s in symbols:
        q = qty.get(s, 0.0)
        exposed = -q * prices[s] if q < 0 else (max(q * prices[s] - share, 0.0) if share else 0.0)         # a short, or the part of a long above 1x (a perpetual), earns / pays funding
        if exposed > 0:
            sign = 1.0 if q < 0 else -1.0
            for rate in get_funding(s, int(last) + 1, now_ms):
                total += sign * exposed * rate
    set_state(con, "last_funding_ms", now_ms)
    return total


def funding_events(symbol: str, since_ms: int, until_ms: int) -> list[float]:
    r = requests.get(binance.FUNDING_URL, params={"symbol": symbol, "startTime": since_ms, "endTime": until_ms, "limit": 100}, timeout=20)
    r.raise_for_status()
    return [float(x["fundingRate"]) for x in r.json()]


# -- execution venues ---------------------------------------------------------------------------------------------------------------------------------------------
def split_venues(q: float, cap_qty: float) -> tuple[float, float]:
    """A signed coin quantity split across the two demo venues: spot holds a LONG up to `cap_qty` (1x the coin's share); a short, or the part of a long above that, is a perpetual position."""
    spot = max(0.0, min(q, cap_qty))
    return spot, q - spot


def perp_legs(perp0: float, delta: float) -> list[tuple[str, float, bool]]:
    """(side, qty, reduce_only) orders that move a perpetual position from perp0 by delta; a move through zero becomes a reduce-only close plus an opening order."""
    if abs(delta) < 1e-12:
        return []
    side = "BUY" if delta > 0 else "SELL"
    legs, remaining = [], abs(delta)
    if perp0 * delta < 0:                                                    # shrinking the existing position first
        closing = min(remaining, abs(perp0))
        legs.append((side, closing, True))
        remaining -= closing
    if remaining > 1e-12:
        legs.append((side, remaining, False))
    return legs


class SimExecutor:
    """Our own simulation of the fills (the fallback when no demo keys are set)."""
    name = "paper"

    def sync(self, con, qty: dict, symbols) -> None:
        return None

    def funding(self, con, symbols, qty, prices, now, get_funding, share) -> float:
        return accrue_funding(con, symbols, qty, prices, now, get_funding, share)

    def execute(self, orders, cash, qty, avg, prices, leverage):
        return apply_orders(cash, qty, avg, prices, orders, COST_SIDE, allow_borrow=leverage > 1.0)


class DemoExecutor:
    """Real market orders on Binance's demo exchange. The exchange is the source of truth for what is held (our quantity = its balance minus the baseline recorded at reset); the sizing ledger
    (cash, equity) is updated from the actual fills, fees and the demo account's own funding payments."""
    name = "binance-demo"

    def __init__(self, client, symbols, leverage_perp: int = 2):
        self.c, self.symbols, self.leverage_perp = client, tuple(symbols), leverage_perp
        self._margin_set: set[str] = set()

    # -- baselines: the demo account may already hold coins; only OUR change counts -------------------------------------------------------------------------------
    def baseline(self, con) -> None:
        for s in self.symbols:
            set_state(con, f"base_spot_{s}", self.c.spot_balance(s[:-4]))
            set_state(con, f"base_perp_{s}", self.c.futures_position(s)["amt"])
        set_state(con, "last_funding_ms", int(time.time() * 1000))
        con.commit()

    def sync(self, con, qty: dict, symbols) -> None:
        from engine import crypto_breakout
        for s in symbols:
            spot = self.c.spot_balance(s[:-4]) - float(get_state(con, f"base_spot_{s}", 0.0))
            # the exchange holds ONE perpetual position per coin, shared with the breakout sleeve (engine/crypto_breakout.py): take its part out
            perp = self.c.futures_position(s)["amt"] - float(get_state(con, f"base_perp_{s}", 0.0)) - crypto_breakout.open_qty(s)
            qty[s] = max(spot, 0.0) + perp

    def funding(self, con, symbols, qty, prices, now, get_funding, share) -> float:
        from engine import crypto_breakout
        since = int(get_state(con, "last_funding_ms", int(now.timestamp() * 1000)))
        total = sum(self.c.futures_income(since + 1, s) for s in symbols)
        set_state(con, "last_funding_ms", int(now.timestamp() * 1000))
        seen, now_total = float(get_state(con, "seen_breakout_funding", crypto_breakout.funding_total())), crypto_breakout.funding_total()
        set_state(con, "seen_breakout_funding", now_total)
        return total - (now_total - seen)                                       # the account's funding includes the breakout sleeve's: not ours

    def _ensure_margin(self, symbol: str) -> None:
        if symbol not in self._margin_set:
            self.c.set_isolated(symbol, self.leverage_perp)
            self._margin_set.add(symbol)

    def execute(self, orders, cash, qty, avg, prices, leverage, share: float = 0.0):
        fills = []
        for o in orders:
            s, px = o["symbol"], prices[o["symbol"]]
            q0 = qty.get(s, 0.0)
            q1 = q0 + (o["qty"] if o["side"] == "BUY" else -o["qty"])
            cap = share / px if share else max(q0, q1, 0.0)
            spot0, perp0 = split_venues(q0, cap)
            spot1, perp1 = split_venues(q1, cap)
            done, fee_total, notional, cash_flow = [], 0.0, 0.0, 0.0
            dspot = spot1 - spot0
            if abs(dspot) > 1e-12:
                f = self.c.filters("spot", s)
                q = min(abs(dspot), spot0) if dspot < 0 else abs(dspot)
                qf = floor_step(q, f["step"])
                if qf >= f["min_qty"] and qf * px >= f["min_notional"]:
                    r = self.c.spot_market(s, "BUY" if dspot > 0 else "SELL", qf)
                    done.append(("spot", r))
                    cash_flow += (-r["notional"] if dspot > 0 else r["notional"]) - r["fee"]
                else:
                    log.info("%s: spot order of %.6g is below the exchange minimum (%.6g coins / %.2f USDT) -- skipped", s, qf, f["min_qty"], f["min_notional"])
            dperp = perp1 - perp0
            from engine import crypto_breakout
            shared = crypto_breakout.open_qty(s) != 0                            # the breakout sleeve also holds this coin: the exchange nets the two, so never reduce-only
            for side, q, reduce_only in perp_legs(perp0, dperp):
                reduce_only = reduce_only and not shared
                f = self.c.filters("futures", s)
                qf = floor_step(q, f["step"])
                if reduce_only:
                    qf = min(qf, floor_step(abs(perp0), f["step"])) if abs(perp0) >= f["min_qty"] else 0.0
                if qf < f["min_qty"] or (qf * px < f["min_notional"] and not reduce_only):
                    log.info("%s: perpetual order of %.6g is below the exchange minimum (%.6g / %.2f USDT) -- skipped", s, qf, f["min_qty"], f["min_notional"])
                    continue
                self._ensure_margin(s)
                r = self.c.futures_market(s, side, qf, reduce_only=reduce_only)
                done.append(("perp", r))
                cash_flow += (-r["notional"] if side == "BUY" else r["notional"]) - r["fee"]
                perp0 += qf if side == "BUY" else -qf
            if not done:
                continue
            for venue, r in done:
                fee_total += r["fee"]
                notional += r["notional"]
            vwap = notional / sum(r["qty"] for _, r in done) if sum(r["qty"] for _, r in done) else px
            cash += cash_flow
            fills.append({**o, "qty": sum(r["qty"] for _, r in done), "price": vwap, "notional": notional, "cost": fee_total, "venues": "+".join(v for v, _ in done)})
        self.sync_after = True
        return cash, fills


def make_executor(symbols, mode: str | None = None):
    """The executor for CRYPTO_EXECUTION (auto = the Binance demo when its keys are set, else our simulation)."""
    mode = (mode or os.environ.get("CRYPTO_EXECUTION", "auto")).lower()
    key, secret = os.environ.get("BINANCE_DEMO_API_KEY", "").strip(), os.environ.get("BINANCE_DEMO_API_SECRET", "").strip()
    if mode == "demo" or (mode == "auto" and key and secret):
        from services.broker.binance_demo import BinanceDemo
        return DemoExecutor(BinanceDemo(key, secret), symbols)
    return SimExecutor()


# -- market data -------------------------------------------------------------------------------------------------------------------------------------------
def hourly(symbol: str) -> pd.DataFrame:
    binance.topup(symbol, "1h")
    df = pd.read_csv(binance.archive_path(symbol, "1h"), parse_dates=["timestamp"]).set_index("timestamp")
    return df.iloc[-(WARMUP_DAYS + 30) * BARS_PER_DAY["1h"]:]


def latest_price(symbol: str) -> float:
    """Close of the most recent CLOSED 15-minute candle (the price the paper fill uses)."""
    r = requests.get(binance.URL, params={"symbol": symbol, "interval": "15m", "limit": 3}, timeout=20)
    r.raise_for_status()
    rows = r.json()
    closed = [x for x in rows if x[6] < time.time() * 1000]
    return float(closed[-1][4])


# -- one cycle --------------------------------------------------------------------------------------------------------------------------------------------
def run_cycle(con: sqlite3.Connection, symbols=DEFAULT_SYMBOLS, now: datetime | None = None, get_hourly=hourly, get_price=latest_price, alert=telegram.send, get_funding=None,
              strategy: str | None = None, executor=None) -> dict:
    now = now or datetime.now(timezone.utc)
    frames = {s: get_hourly(s) for s in symbols}
    prices = {s: get_price(s) for s in symbols}
    qty = holdings(con)
    avg = {s: a for s, _, a in con.execute("SELECT symbol, qty, avg_price FROM holdings")}
    cash = float(get_state(con, "cash", get_state(con, "capital", "10000")))
    capital = float(get_state(con, "capital", "10000"))
    for s in symbols:
        if s not in qty:
            qty[s] = 0.0
    strategy = strategy or get_state(con, "strategy") or os.environ.get("CRYPTO_STRATEGY", "blend")
    executor = executor or SimExecutor()
    leverage = float(get_state(con, "leverage", os.environ.get("CRYPTO_LEVERAGE", "1.0")))
    executor.sync(con, qty, symbols)                                                    # the demo exchange is the source of truth for what is held
    equity = cash + sum(qty[s] * prices[s] for s in symbols)
    cash += executor.funding(con, symbols, qty, prices, now, get_funding, equity / max(len(symbols), 1))       # funding earned / paid by the perpetual positions since the last cycle
    equity = cash + sum(qty[s] * prices[s] for s in symbols)
    if get_state(con, "start_prices") is None:                                          # buy-and-hold baseline: an equal split into the same coins on day one
        set_state(con, "start_prices", ",".join(f"{s}={prices[s]}" for s in symbols))
    start = dict(kv.split("=") for kv in get_state(con, "start_prices").split(","))
    buy_hold = capital * sum(prices[s] / float(start[s]) for s in symbols if s in start) / max(len(start), 1)

    last_bar = str(max(f.index[-1] for f in frames.values()))
    decided = get_state(con, "last_decision_bar")
    fills, targets = [], {}
    if decided != last_bar:                                                              # a new hourly bar has closed: decide
        if strategy in ("router", "blend") and "BTCUSDT" in frames:
            targets, regime = router.current_targets(frames, router_weight=1.0 if strategy == "router" else float(os.environ.get("CRYPTO_ROUTER_WEIGHT", "0.5")))
        else:
            targets, regime = {s: float(ensemble_position(frames[s].loc[:frames[s].index[-1]])[-1]) for s in symbols}, "n/a"
        set_state(con, "regime", regime)
        orders = plan_trades(equity, prices, qty, {s: t * leverage for s, t in targets.items()}, band=float(os.environ.get("CRYPTO_REBALANCE_BAND", "0.10")), max_abs=leverage)
        if isinstance(executor, DemoExecutor):
            cash, fills = executor.execute(orders, cash, qty, avg, prices, leverage, share=equity / max(len(symbols), 1))
            executor.sync(con, qty, symbols)                                            # re-read what the fills actually left us holding
        else:
            cash, fills = executor.execute(orders, cash, qty, avg, prices, leverage)
        stamp = now.isoformat(timespec="seconds")
        for f in fills:
            con.execute("INSERT INTO trades(ts, symbol, side, qty, price, notional, cost, target, reason) VALUES(?,?,?,?,?,?,?,?,?)",
                        (stamp, f["symbol"], f["side"], f["qty"], f["price"], f["notional"], f["cost"], targets.get(f["symbol"]), f["reason"]))
        for s in symbols:
            con.execute("INSERT INTO holdings(symbol, qty, avg_price) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price", (s, qty[s], avg.get(s, 0.0)))
        set_state(con, "cash", cash)
        set_state(con, "last_decision_bar", last_bar)
        set_state(con, "last_targets", ",".join(f"{s}={t:.3f}" for s, t in targets.items()))
        equity = cash + sum(qty[s] * prices[s] for s in symbols)
        log.info("decision on the %s bar (regime %s): targets %s; %d trade(s)", last_bar, regime, {s: round(t, 2) for s, t in targets.items()}, len(fills))
        if fills:
            alert(f"<b>CRYPTO {executor.name}</b> ({leverage:g}x, regime {regime}) " + "; ".join(f"{f['side']} {f['qty']:.5g} {f['symbol']} @ {f['price']:,.2f} ({f['reason']})" for f in fills) + f"\nEquity {equity:,.0f} USDT vs buy&hold {buy_hold:,.0f}")
    import json
    con.execute("INSERT OR REPLACE INTO equity(ts, equity, cash, buy_hold, holdings_json) VALUES(?,?,?,?,?)",
                (now.isoformat(timespec="seconds"), equity, cash, buy_hold, json.dumps({s: qty[s] for s in symbols})))
    con.commit()
    set_state(con, "strategy", strategy)
    con.commit()
    return {"equity": equity, "cash": cash, "buy_hold": buy_hold, "fills": fills, "targets": targets, "bar": last_bar, "prices": prices}


def status(con) -> str:
    capital = float(get_state(con, "capital", "0") or 0)
    if not capital:
        return "No crypto paper account yet (run: python3 -m engine.crypto_paper --once)"
    q = holdings(con)
    last = con.execute("SELECT ts, equity, cash, buy_hold FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    lines = [f"CRYPTO account (started with {capital:,.0f} USDT, strategy {get_state(con, 'strategy', '?')}, leverage {get_state(con, 'leverage', '1.0')}x, executed on: {get_state(con, 'execution', 'paper')}; BTC/ETH/SOL; regime now: {get_state(con, 'regime', '?')})"]
    if last:
        ts, eq, cash, bh = last
        lines.append(f"  {ts}: equity {eq:,.2f} USDT ({(eq / capital - 1) * 100:+.2f}%), cash {cash:,.2f}, buy&hold of the same coins {bh:,.2f} ({(bh / capital - 1) * 100:+.2f}%)")
    for s, n in q.items():
        lines.append(f"  {'SHORT (perp)' if n < 0 else 'long':12s} {s}: {n:.6g}")
    lines.append(f"  targets at the last decision: {get_state(con, 'last_targets', 'none yet')}   (bar {get_state(con, 'last_decision_bar', '-')})")
    n_trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    lines.append(f"  trades so far: {n_trades}")
    return "\n".join(lines)


def demo_check(symbols, smoke: bool = False) -> int:
    """Read-only check of the Binance DEMO account (and, with --demo-smoke, tiny round-trip orders on the demo exchange)."""
    from services.broker.binance_demo import BinanceDemo, BinanceError
    key, secret = os.environ.get("BINANCE_DEMO_API_KEY", "").strip(), os.environ.get("BINANCE_DEMO_API_SECRET", "").strip()
    if not key or not secret:
        print("BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET are not set in backend/.env.\nCreate them at https://demo.binance.com/en/my/settings/api-management (Demo Trading -> API Key Management), enable trading, and paste both into .env.")
        return 2
    c = BinanceDemo(key, secret)
    try:
        for s in symbols:
            sp, fu = c.filters("spot", s), c.filters("futures", s)
            print(f"{s}: spot balance {c.spot_balance(s[:-4]):.6g} {s[:-4]} (min order {sp['min_notional']:g} USDT, step {sp['step']:g}) | perp position {c.futures_position(s)['amt']:.6g} (min order {fu['min_notional']:g} USDT, step {fu['step']:g}) | price {c.price('spot', s):,.2f}")
        print(f"spot USDT balance {c.spot_balance('USDT'):,.2f} | futures wallet {c.futures_wallet()}")
        if smoke:
            s = symbols[-1]
            px = c.price("spot", s)
            fs, ff = c.filters("spot", s), c.filters("futures", s)
            q = floor_step(max(fs["min_notional"], ff["min_notional"]) * 1.5 / px, max(fs["step"], ff["step"]))
            print(f"smoke test on {s}: {q} coins (~{q * px:.2f} USDT)")
            buy = c.spot_market(s, "BUY", q)
            print("  spot BUY ", buy)
            got = floor_step(c.spot_balance(s[:-4]), fs["step"])                       # the fee is taken in the coin, so sell what is actually held
            print("  spot SELL", c.spot_market(s, "SELL", got))
            c.set_isolated(s, 2)
            print("  perp SELL (open short)", c.futures_market(s, "SELL", q))
            print("  perp BUY  (close, reduce-only)", c.futures_market(s, "BUY", q, reduce_only=True))
            print("demo order path OK")
    except BinanceError as exc:
        print(f"Binance demo error: {exc}")
        return 1
    return 0


# -- daemon -------------------------------------------------------------------------------------------------------------------------------------------------
def _next_wake(now: datetime) -> float:
    """Seconds until 20 s past the next quarter hour (the 1-hour bar is final by then)."""
    nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)).replace(second=20)
    return max((nxt - now).total_seconds(), 5.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--demo-check", action="store_true", help="verify the Binance demo keys and show the demo account (read-only)")
    ap.add_argument("--demo-smoke", action="store_true", help="place and reverse tiny spot and perpetual orders on the DEMO exchange to prove the order path works")
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    load_dotenv(BACKEND_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    symbols = tuple(os.environ.get("CRYPTO_SYMBOLS", " ".join(DEFAULT_SYMBOLS)).split())
    capital = a.capital or float(os.environ.get("CRYPTO_CAPITAL_USDT", "10000"))
    if a.demo_check or a.demo_smoke:
        return demo_check(symbols, smoke=a.demo_smoke)
    executor = make_executor(symbols)

    def new_account(con):
        reset(con, capital, symbols)
        set_state(con, "strategy", os.environ.get("CRYPTO_STRATEGY", "blend"))
        set_state(con, "leverage", os.environ.get("CRYPTO_LEVERAGE", "1.0"))
        set_state(con, "execution", executor.name)
        if isinstance(executor, DemoExecutor):
            executor.baseline(con)
        con.commit()

    with closing(connect()) as con:
        if a.status:
            print(status(con))
            return 0
        if a.reset:
            new_account(con)
            print(f"crypto account reset with {capital:,.0f} USDT (strategy {os.environ.get('CRYPTO_STRATEGY', 'blend')}, leverage {os.environ.get('CRYPTO_LEVERAGE', '1.0')}x, executed on {executor.name})")
            return 0
        if get_state(con, "capital") is None:
            new_account(con)
        if os.environ.get("ENABLE_CRYPTO_TRADING", "true").lower() not in ("1", "true", "yes"):
            print("ENABLE_CRYPTO_TRADING is off")
            return 0
        from engine import crypto_breakout
        breakout_on = os.environ.get("CRYPTO_BREAKOUT_ENABLED", "false").lower() in ("1", "true", "yes")
        bcon = crypto_breakout.connect() if breakout_on else None
        if bcon is not None and crypto_breakout.get_state(bcon, "capital") is None:
            crypto_breakout.reset(bcon, float(os.environ.get("CRYPTO_BREAKOUT_CAPITAL_USDT", os.environ.get("CRYPTO_CAPITAL_USDT", "500"))))
        bfills = (crypto_breakout.DemoFills(executor.c) if isinstance(executor, DemoExecutor) else crypto_breakout.SimFills()) if breakout_on else None

        def breakout_cycle():
            # AFTER the blend, in the same process: the blend has just read the exchange with this sleeve's last known quantity subtracted
            if bcon is not None:
                try:
                    crypto_breakout.run_cycle(bcon, symbols, bfills)
                except Exception as exc:
                    log.error("breakout cycle failed: %s", exc)

        if a.once:
            r = run_cycle(con, symbols, executor=executor)
            breakout_cycle()
            print(status(con))
            if bcon is not None:
                print(crypto_breakout.status(bcon))
            return 0
        DB_DIR.mkdir(parents=True, exist_ok=True)
        lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another crypto paper daemon is already running")
            return 1
        stop = {"now": False}
        os_signal.signal(os_signal.SIGTERM, lambda *_: stop.update(now=True))
        log.info("crypto trading started: %s, %s USDT, executed on %s, leverage %sx", ", ".join(symbols), f"{capital:,.0f}", executor.name, get_state(con, "leverage", "1.0"))
        telegram.send(f"<b>CRYPTO trading started</b> — {', '.join(symbols)}, {get_state(con, 'strategy', 'blend')} strategy, {get_state(con, 'leverage', '1.0')}x, executed on {executor.name}.")
        if bcon is not None:
            log.info("breakout sleeve on: %s USDT, executed on %s", crypto_breakout.get_state(bcon, "capital"), bfills.name)
        while not stop["now"]:
            try:
                run_cycle(con, symbols, executor=executor)
            except Exception as exc:                                    # a network blip must not kill the daemon; the next cycle retries
                log.error("cycle failed: %s", exc)
            breakout_cycle()
            wake = time.monotonic() + _next_wake(datetime.now(timezone.utc))
            while not stop["now"] and time.monotonic() < wake:          # a stop request (systemctl stop/restart) is honoured within a second, not after the whole wait
                time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
