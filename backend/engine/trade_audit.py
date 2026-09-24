"""Audit closed paper trades against the exchange's own candles.

    python3 -m engine.trade_audit [--date YYYY-MM-DD | --days N] [--db PATH] [--no-telegram]

Why: on 2026-09-23 four trades were stopped out ~35 seconds after entry at prices that never traded after the entry (the exit check was
reading pre-entry lows). Nothing flagged it; it was found by hand, twice. This makes that check automatic. For every closed trade it
loads the finest archived candles (1-minute, else 3, else 5) and verifies, over the entry bar through the exit bar (a superset of what
the trade saw, so a flag is always a genuine problem; see audit_trade):

  * a stop / trail exit price was actually traded through after entry            (phantom-stop detector)
  * a take-profit price was actually reached                                      (phantom-target detector)
  * a breakeven / trail exit had really armed (price reached the breakeven level)
  * a timeout / square-off / manual exit price lies inside the exit bar's range
  * the entry price was inside the recent traded range                            (fill sanity; also reports the fill offset in bps)

A trade with no candle coverage is reported as 'no-data', never as a pass. Exit code 1 if any trade is flagged; Telegram alert too.
Runs nightly right after the data top-up (deploy/systemd/hft-daily-data-topup.service), when yesterday's 1-minute bars are complete.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from core.paths import ARCHIVE_ROOT, DB_DIR

STATE_PATH = DB_DIR / "audit_state.json"    # position_ids already alerted on, so a re-audit of the same days never repeats an alert
DEFAULT_DAYS = 3                                 # re-audit the last few days each night: a day whose candles were not final yet is picked up next run
TOL = 0.0005                     # 5 bps: exits are recorded at the stop price; this only absorbs tick rounding
ENTRY_LOOKBACK_MIN = 10          # the entry is priced off the last CLOSED 5-minute bar, so look back a little
COMMODITY_STEM = {"GOLDM": "GOLD", "NATGASMINI": "NATURALGAS"}     # traded symbol -> archive file prefix (see markets/commodity/data.py SYMBOLS)
STOP_LIKE = {"initial_stop", "be_stop", "trail_stop", "stop_loss", "stop"}
BREAKEVEN_LIKE = {"be_stop", "trail_stop"}
TARGET_LIKE = {"take_profit", "target"}


@dataclass
class Finding:
    trade_id: int
    symbol: str
    kind: str                    # phantom_exit | phantom_target | unarmed_breakeven | exit_outside_bar | entry_off_market | no_data
    detail: str

    @property
    def flagged(self) -> bool:
        return self.kind != "no_data"


@dataclass
class TradeAudit:
    trade: dict
    findings: list[Finding] = field(default_factory=list)
    fill_offset_bps: float | None = None
    granularity: str = ""


def _archive_path(symbol: str, market: str, suffix: str):
    stem = COMMODITY_STEM.get(symbol, symbol) if market == "commodity" else symbol
    return ARCHIVE_ROOT / market / f"{stem}_{suffix}.csv"


def load_candles(symbol: str, market: str, day: date) -> tuple[pd.DataFrame | None, str]:
    """Finest archived candles covering `day` (1-minute, else 3, else 5). Returns (frame with tz-aware 'ts', suffix)."""
    for suffix in ("1minute", "3minute", "5minute"):
        path = _archive_path(symbol, market, suffix)
        if not path.exists():
            continue
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["timestamp"])
        df = df[df["ts"].dt.strftime("%Y-%m-%d") == day.isoformat()]
        if len(df):
            return df.sort_values("ts").reset_index(drop=True), suffix
    return None, ""


def audit_trade(trade: dict, candles: pd.DataFrame | None, suffix: str = "1minute") -> TradeAudit:
    """`trade` needs: trade_id, symbol, direction, entry_price, exit_price, entry_dt, exit_dt, exit_reason
    and (optional) breakeven_price, target_price."""
    out = TradeAudit(trade, granularity=suffix)
    tid, sym = trade["trade_id"], trade["symbol"]
    if candles is None or candles.empty:
        out.findings.append(Finding(tid, sym, "no_data", "no archived candles cover this trade"))
        return out
    long_ = trade["direction"] == "long"
    entry, exit_p, why = float(trade["entry_price"]), float(trade["exit_price"]), trade["exit_reason"]
    step = {"1minute": 1, "3minute": 3, "5minute": 5}[suffix]
    e_ts, x_ts = pd.Timestamp(trade["entry_dt"]), pd.Timestamp(trade["exit_dt"])
    tz = candles["ts"].dt.tz
    if tz is not None and e_ts.tzinfo is None:
        e_ts, x_ts = e_ts.tz_localize(tz), x_ts.tz_localize(tz)
    e_bar = e_ts.floor(f"{step}min")
    # The entry bar's high/low include prices from BEFORE the entry, so this window is a SUPERSET of what the trade could have seen.
    # That keeps every check below one-sided and sound: if even the superset never traded through a stop, the stop was certainly
    # phantom; the superset can only ever hide a phantom (a miss), never invent one (a false alarm).
    post = candles[(candles["ts"] >= e_bar) & (candles["ts"] <= x_ts)]
    if post.empty:
        out.findings.append(Finding(tid, sym, "no_data", "no candles between entry and exit"))
        return out
    lo, hi = float(post["low"].min()), float(post["high"].max())

    # -- exit price must have traded after entry --
    if why in STOP_LIKE:
        traded = lo <= exit_p * (1 + TOL) if long_ else hi >= exit_p * (1 - TOL)
        if not traded:
            out.findings.append(Finding(tid, sym, "phantom_exit",
                                        f"{why} at {exit_p} but after entry price only ranged {lo}-{hi} ({len(post)} bars)"))
    elif why in TARGET_LIKE:
        traded = hi >= exit_p * (1 - TOL) if long_ else lo <= exit_p * (1 + TOL)
        if not traded:
            out.findings.append(Finding(tid, sym, "phantom_target", f"take-profit at {exit_p} never reached (range {lo}-{hi})"))
    else:
        x_bar = candles[(candles["ts"] == x_ts.floor(f"{step}min"))]
        if len(x_bar) and not (float(x_bar["low"].iloc[0]) * (1 - TOL) <= exit_p <= float(x_bar["high"].iloc[0]) * (1 + TOL)):
            out.findings.append(Finding(tid, sym, "exit_outside_bar",
                                        f"{why} at {exit_p} outside the exit bar's range {x_bar['low'].iloc[0]}-{x_bar['high'].iloc[0]}"))

    # -- a breakeven / trail exit implies the breakeven level was reached after entry --
    be = trade.get("breakeven_price")
    if why in BREAKEVEN_LIKE and be:
        reached = hi >= float(be) * (1 - TOL) if long_ else lo <= float(be) * (1 + TOL)
        if not reached:
            out.findings.append(Finding(tid, sym, "unarmed_breakeven", f"{why} but breakeven level {be} never reached (range {lo}-{hi})"))

    # -- entry sanity and fill offset --
    recent = candles[(candles["ts"] >= e_bar - pd.Timedelta(minutes=ENTRY_LOOKBACK_MIN)) & (candles["ts"] <= e_bar)]
    if len(recent):
        if not (float(recent["low"].min()) * (1 - TOL) <= entry <= float(recent["high"].max()) * (1 + TOL)):
            out.findings.append(Finding(tid, sym, "entry_off_market",
                                        f"entry {entry} outside the traded range {recent['low'].min()}-{recent['high'].max()} of the last {ENTRY_LOOKBACK_MIN} min"))
        mkt = candles[candles["ts"] == e_bar]
        if len(mkt):
            out.fill_offset_bps = (entry / float(mkt["close"].iloc[0]) - 1) * 1e4 * (1 if long_ else -1)      # + = paper filled WORSE than the bar's close
    return out


def fetch_trades(db_path, day_from: date, day_to: date) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT t.trade_id, t.position_id, t.symbol, t.direction, t.entry_price, t.exit_price, t.entry_dt, t.exit_dt, t.exit_reason, "
            "       p.breakeven_price, p.target_price "
            "FROM trades t LEFT JOIN positions p ON p.position_id = t.position_id "
            "WHERE date(t.exit_dt) BETWEEN ? AND ? ORDER BY t.trade_id", (day_from.isoformat(), day_to.isoformat())).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def run_audit(trades: list[dict], loader=load_candles) -> list[TradeAudit]:
    from core.registry import market_of
    cache: dict[tuple, tuple] = {}
    results = []
    for t in trades:
        day = datetime.fromisoformat(str(t["entry_dt"])).date()
        key = (t["symbol"], day)
        if key not in cache:
            cache[key] = loader(t["symbol"], market_of(t["symbol"]), day)
        candles, suffix = cache[key]
        results.append(audit_trade(t, candles, suffix or "1minute"))
    return results


def format_report(results: list[TradeAudit]) -> tuple[str, int]:
    flagged = [f for r in results for f in r.findings if f.flagged]
    no_data = [f for r in results for f in r.findings if not f.flagged]
    offs = [r.fill_offset_bps for r in results if r.fill_offset_bps is not None]
    lines = [f"Trade audit: {len(results)} trade(s), {len(flagged)} flagged, {len(no_data)} without candle data."]
    for f in flagged:
        lines.append(f"  FLAG #{f.trade_id} {f.symbol}: {f.kind} -- {f.detail}")
    for f in no_data:
        lines.append(f"  no-data #{f.trade_id} {f.symbol}: {f.detail}")
    if offs:
        lines.append(f"  entry fill vs bar close: mean {sum(offs) / len(offs):+.1f} bps, worst {max(offs):+.1f} bps over {len(offs)} trades (+ = paper filled worse)")
    return "\n".join(lines), len(flagged)


def _load_state(path) -> set:
    try:
        return set(json.loads(open(path).read()).get("alerted", []))
    except (OSError, ValueError):
        return set()


def _save_state(path, alerted: set) -> None:
    try:
        with open(path, "w") as fh:
            json.dump({"alerted": sorted(alerted)}, fh)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="audit trades that exited on this date (default: yesterday IST)")
    g.add_argument("--days", type=int, help=f"audit trades that exited in the last N days including today (default {DEFAULT_DAYS})")
    ap.add_argument("--db", default=str(DB_DIR / "paper_trading.db"))
    ap.add_argument("--state", default=str(STATE_PATH), help="file remembering which trades were already alerted")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args(argv)
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if args.date:
        d0 = d1 = date.fromisoformat(args.date)
    elif args.days:
        d0, d1 = today - timedelta(days=args.days - 1), today
    else:
        d0, d1 = today - timedelta(days=DEFAULT_DAYS - 1), today
    results = run_audit(fetch_trades(args.db, d0, d1))
    text, n_flagged = format_report(results)
    print(f"{d0}..{d1}\n{text}")
    if n_flagged and not args.no_telegram:
        seen = _load_state(args.state)
        new = [r for r in results if any(f.flagged for f in r.findings) and r.trade["position_id"] not in seen]
        if new:
            from services.utils import telegram
            new_text, _ = format_report(new)
            telegram.send(f"🔎 <b>TRADE AUDIT</b> ({d0}..{d1})\n{new_text}")
            _save_state(args.state, seen | {r.trade["position_id"] for r in new})
    return 1 if n_flagged else 0


if __name__ == "__main__":
    sys.exit(main())
