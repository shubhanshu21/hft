"""Backtest any daily-bar Strategy (core/strategy.py) on the archived candles, for the web "New backtest" page.

    from core.backtester import run_backtest
    result = run_backtest(strategy, ["RELIANCE", "TCS"], "2024-01-01", "2026-10-09")

The strategy only DECIDES (entry / manage / costs), exactly as in paper trading. This engine owns the account:
  * a signal is decided on the close of day t and filled at the OPEN of day t+1, with slippage against us;
  * a stop fills with core.exits.bar_exit (a bar that opens through the stop fills at the open, same-bar stop and target -> stop first);
  * costs come from the strategy's own costs();
  * one account, compounding, one position per symbol, at most `max_positions` open at once, sized so the notional never exceeds the free equity (times leverage).
The result is plain JSON: the same shape the frontend replay reads (days, equity, events, pos, prices) plus metrics.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Callable

import pandas as pd

from core.exits import bar_exit
from core.paths import ARCHIVE_ROOT
from core.strategy import EntryContext, ExitContext, Strategy

DAILY_FILE = {"equity": "equity/{sym}_daily.csv", "currency": "currency/{sym}_1day.csv"}


class BacktestError(ValueError):
    """The request cannot be run (bad dates, no data, unsupported strategy). The message is shown to the user."""


def load_daily(market: str, symbol: str) -> pd.DataFrame:
    path = ARCHIVE_ROOT / DAILY_FILE[market].format(sym=symbol)
    if not path.exists():
        raise BacktestError(f"No daily data for {symbol}")
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["timestamp"].str[:10])
    return df.drop_duplicates("date").set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]


def _candles(df: pd.DataFrame) -> list[dict]:
    return [{"timestamp": d.strftime("%Y-%m-%dT00:00:00+05:30"), "open": float(r.open), "high": float(r.high), "low": float(r.low),
             "close": float(r.close), "volume": float(r.volume)} for d, r in df.iterrows()]


def run_backtest(strategy: Strategy, symbols: list[str], start: str, end: str, *, capital: float = 100_000.0, slippage_bp: float = 5.0,
                 risk_pct: float = 2.0, max_positions: int = 3, leverage: float = 1.0,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    if tuple(strategy.timeframe) != ("days", 1):
        raise BacktestError(f"{strategy.market}/{strategy.name} reads {strategy.timeframe} bars; the web backtester runs daily-bar strategies only")
    if strategy.market not in DAILY_FILE:
        raise BacktestError(f"The web backtester has no daily data layout for market {strategy.market!r}")
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    if t1 <= t0:
        raise BacktestError("The end date must be after the start date")
    frames, skipped = {}, []
    for s in symbols:
        try:
            frames[s] = load_daily(strategy.market, s)
        except BacktestError:
            skipped.append(s)
    if not frames:
        raise BacktestError("None of the chosen symbols has daily data")
    cache = {s: _candles(df) for s, df in frames.items()}
    pos_of = {s: {d: i for i, d in enumerate(df.index)} for s, df in frames.items()}
    days = sorted({d for df in frames.values() for d in df.index if t0 <= d <= t1})
    if len(days) < 5:
        raise BacktestError("Fewer than 5 trading days with data in that range")
    window = max(int(strategy.lookback_days or 0), 120)
    lot = {s: max(int(strategy.lot_size(s)), 1) for s in frames}
    slip = slippage_bp / 1e4

    cash = float(capital)                      # realised money
    book: dict[str, dict] = {}                 # open positions
    pending: list[dict] = []                   # signals decided yesterday, filled today
    closed: list[dict] = []
    out = {"days": [], "equity": [], "events": [], "pos": []}
    day_index = {d: i for i, d in enumerate(days)}
    last_close = {s: None for s in frames}

    def equity_at(close_of: dict) -> float:
        return cash + sum((close_of[s] - p["entry_price"]) * p["dir"] * p["qty"] * lot[s] for s, p in book.items() if close_of.get(s))

    for n, d in enumerate(days):
        events: list[dict] = []
        opens = {s: float(df.at[d, "open"]) for s, df in frames.items() if d in df.index}
        closes = {s: float(df.at[d, "close"]) for s, df in frames.items() if d in df.index}
        for s, c in closes.items():
            last_close[s] = c
        mark = {s: closes.get(s, last_close[s]) for s in frames}

        # 1. fill yesterday's signals at today's open
        eq_open = cash + sum((opens.get(s, p["entry_price"]) - p["entry_price"]) * p["dir"] * p["qty"] * lot[s] for s, p in book.items())
        for sig in pending:
            s = sig["symbol"]
            if s in book or len(book) >= max_positions or s not in opens:
                continue
            dr = 1 if sig["direction"] == "long" else -1
            fill = opens[s] * (1 + slip * dr)
            if (dr == 1 and fill <= sig["stop"]) or (dr == -1 and fill >= sig["stop"]):
                continue                                           # gapped through the stop: no trade
            used = sum(p["entry_price"] * p["qty"] * lot[p_s] for p_s, p in book.items()) / leverage
            qty = min(sig["qty"], int(max(eq_open - used, 0) * leverage / (fill * lot[s])), int(eq_open * leverage / max_positions / (fill * lot[s])))
            if qty < 1:
                continue
            book[s] = {"direction": sig["direction"], "dir": dr, "qty": qty, "entry_price": fill, "current_stop": sig["stop"], "best_price": fill,
                       "stop_dist": abs(fill - sig["stop"]), "tp": sig["tp"] or None, "ref_open": opens[s], "entry_time": datetime(d.year, d.month, d.day), "entry_i": n, "symbol": s,
                       **sig["state"]}
            events.append({"t": "buy", "sym": s, "q": qty * lot[s], "px": round(fill, 4), "note": sig["note"]})
        pending = []

        # 2. manage open positions on today's bar
        for s in list(book):
            if s not in opens:
                continue
            p, row = book[s], frames[s].loc[d]
            fav, adv = (float(row.high), float(row.low)) if p["dir"] == 1 else (float(row.low), float(row.high))
            hit = bar_exit(p["current_stop"], p["tp"], float(row.open), fav, adv, p["dir"])
            price, why = (hit[0], "Stop hit" if hit[1] == "stop" else "Target hit") if hit else (None, None)
            if hit is None:
                p["best_price"] = max(p["best_price"], fav) if p["dir"] == 1 else min(p["best_price"], fav)
                i = pos_of[s][d]
                dec = strategy.manage(p, ExitContext(symbol=s, candles=cache[s][max(0, i + 1 - window): i + 1], now=datetime(d.year, d.month, d.day), data=None))
                if dec is not None:
                    price, why = float(dec.price), dec.reason.replace("_", " ").capitalize()
            if price is None:
                continue
            exit_px = price * (1 - slip * p["dir"])
            cost = trade_costs(strategy, s, p["direction"], p["entry_price"], exit_px, p["qty"])
            gross = (exit_px - p["entry_price"]) * p["dir"] * p["qty"] * lot[s]
            fees = float(cost.get("total", gross - cost["net"]))
            net = float(cost.get("net", gross - fees))
            slip_cost = (abs(p["entry_price"] - p["ref_open"]) + abs(exit_px - price)) * p["qty"] * lot[s]
            cash += net
            events.append({"t": "sell", "sym": s, "q": p["qty"] * lot[s], "px": round(exit_px, 4), "pnl": round(net), "fees": round(fees), "slip": round(slip_cost),
                           "days": n - p["entry_i"], "why": why, "entry": round(p["entry_price"], 4)})
            closed.append({"net": net, "fees": fees, "slip": slip_cost, "gross": gross, "days": n - p["entry_i"]})
            del book[s]

        # 3. decide tomorrow's entries from today's close
        if n < len(days) - 1:
            equity_now = equity_at(mark)
            for s in frames:
                if s in book or s not in closes or not strategy.trades(s):
                    continue
                i = pos_of[s][d]
                if i < 30:
                    continue
                sig = strategy.entry(EntryContext(symbol=s, candles=cache[s][max(0, i + 1 - window): i + 1], now=datetime(d.year, d.month, d.day),
                                                  instrument_key=None, capital=equity_now, risk_pct=risk_pct, leverage=leverage, direction_filter="both"))
                if sig is None or sig.qty < 1 or (sig.direction == "short" and not strategy.allow_short):
                    continue
                pending.append({"symbol": s, "direction": sig.direction, "qty": sig.qty, "stop": sig.stop_loss, "tp": sig.target_price or None,
                                "state": dict(sig.exit_state), "note": sig.setup_type.replace("_", " ")})

        out["days"].append(d.strftime("%Y-%m-%d"))
        out["equity"].append(round(equity_at(mark)))
        out["events"].append(events)
        out["pos"].append([{"sym": s, "q": p["qty"] * lot[s], "e": round(p["entry_price"], 4), "px": round(mark[s], 4), "start": p["entry_i"]} for s, p in book.items()])
        if progress and n % 10 == 0:
            progress(n, len(days))

    # still open at the end: marked at the last close, not counted as closed
    traded = sorted({e["sym"] for evs in out["events"] for e in evs} | set(book))
    out["bars"] = {s: [_bar(frames[s], d) for d in days] for s in traded}            # [open, high, low, close] per day, null before the first candle / on a holiday
    out["metrics"] = metrics(out["equity"], capital, closed, len(days), len(book))
    out["skipped_symbols"] = skipped
    return out


def _bar(df: pd.DataFrame, d: pd.Timestamp):
    if d not in df.index:
        return None
    r = df.loc[d]
    return [round(float(r.open), 4), round(float(r.high), 4), round(float(r.low), 4), round(float(r.close), 4)]


def trade_costs(strategy: Strategy, symbol: str, direction: str, entry: float, exit_px: float, qty: int) -> dict:
    """Fees and taxes of one round trip. The strategy's own costs() when it has one; otherwise the market's delivery cost model, so a
    backtest is never run fee-free."""
    try:
        return strategy.costs(symbol, direction, entry, exit_px, qty)
    except NotImplementedError:
        if strategy.market == "equity":
            from markets.equity.costs import compute_nse_equity_delivery_costs
            return compute_nse_equity_delivery_costs(direction, entry, exit_px, qty, symbol=symbol)
        from markets.currency.costs import compute_ncd_currency_costs
        return compute_ncd_currency_costs(symbol, direction, entry, exit_px, qty)


def metrics(equity: list[int], capital: float, closed: list[dict], n_days: int, still_open: int) -> dict:
    final = equity[-1]
    wins = [c for c in closed if c["net"] > 0]
    gross_win = sum(c["net"] for c in wins)
    gross_loss = -sum(c["net"] for c in closed if c["net"] <= 0)
    peak, dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    years = max(n_days / 250, 1e-9)
    return {
        "start_capital": round(capital), "final_equity": final, "return_pct": round((final / capital - 1) * 100, 2),
        "cagr_pct": round(((final / capital) ** (1 / years) - 1) * 100, 2) if final > 0 else -100.0,
        "max_drawdown_pct": round(dd * 100, 2), "trades": len(closed), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not closed else math.inf if gross_win else 0.0),
        "gross_profit": round(sum(c["gross"] for c in closed)), "fees": round(sum(c["fees"] for c in closed)),
        "slippage": round(sum(c["slip"] for c in closed)), "avg_hold_days": round(sum(c["days"] for c in closed) / len(closed), 1) if closed else 0.0,
        "open_at_end": still_open, "trading_days": n_days,
    }
