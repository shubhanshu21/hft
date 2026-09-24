"""Deterministic replay of DryRunner.scan() over recorded market data.

Used to prove refactors change no trading behaviour: run the harness, freeze the output as a
golden file (tests/golden/replay.json), and require later versions to reproduce it exactly.

Everything non-deterministic is pinned: time is the recorded bar time, candles come from the
archive CSVs, the spread log is disabled (flat slippage), Telegram is captured not sent, and the
DB is a throwaway SQLite file.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from core.paths import ARCHIVE_ROOT

IST = timezone(timedelta(hours=5, minutes=30))

# (archive market dir, archive file stem, traded symbol)
INSTRUMENTS = [
    ("commodity", "CRUDEOIL", "CRUDEOILM"), ("commodity", "GOLD", "GOLDM"), ("commodity", "SILVER", "SILVER"),
    ("currency", "USDINR", "USDINR"),
    ("equity", "TATASTEEL", "TATASTEEL"), ("equity", "RELIANCE", "RELIANCE"), ("equity", "SBIN", "SBIN"),
    ("equity", "ICICIBANK", "ICICIBANK"), ("equity", "INFY", "INFY"), ("equity", "HINDALCO", "HINDALCO"),
    ("equity", "ADANIENT", "ADANIENT"), ("equity", "JSWSTEEL", "JSWSTEEL"), ("equity", "BAJFINANCE", "BAJFINANCE"),
    ("equity", "AXISBANK", "AXISBANK"), ("equity", "M&M", "M&M"), ("equity", "COALINDIA", "COALINDIA"),
]
DAYS = ["2026-08-26", "2026-08-27", "2026-09-01", "2026-09-03", "2026-09-08", "2026-09-10", "2026-09-15", "2026-09-17"]

QUICK_DAYS = ["2026-09-01", "2026-09-10"]
QUICK_INSTRUMENTS = [i for i in INSTRUMENTS if i[2] in ("CRUDEOILM", "GOLDM", "USDINR", "TATASTEEL", "SBIN", "JSWSTEEL")]

# Loosened entry thresholds (identically for whichever code is being tested) so the replay produces
# plenty of entries and exercises every exit path, the caps and the cooldowns. Real thresholds are
# strict enough that a two-day window can contain no trade at all, which would prove nothing.
PERMISSIVE = {"min_adx": 8.0, "min_vol": 0.6, "min_ema_slope": 0.002, "min_orb": 0.0, "min_vwap": 0.0}

ENV = {
    "USE_COMMODITY_REGIME_FILTER": "false", "USE_CRUDE_REGIME_FILTER": "false", "USE_EQUITY_REGIME_FILTER": "false",
    "ENABLE_MEAN_REVERSION": "false", "DRYRUN_INCLUDE_EQUITY": "true", "DRYRUN_FULL_SESSION": "true",
    "ENABLE_COMMODITY_TRADING": "true", "ENABLE_CURRENCY_TRADING": "true", "ENABLE_EQUITY_TRADING": "true",
    "COMMODITY_RISK_PCT": "4.0", "COMMODITY_LEVERAGE": "5.0", "CURRENCY_RISK_PCT": "4.0", "CURRENCY_LEVERAGE": "5.0",
    "EQUITY_RISK_PCT": "4.0", "EQUITY_LEVERAGE": "5.0",
    "CRUDEOILM_RISK_PCT": "3.0", "SILVER_RISK_PCT": "5.0",       # the coded per-symbol values; pinned so the golden never reads the operator's .env
    "MAX_PORTFOLIO_HEAT_PCT": "12.0", "MAX_DAILY_LOSS_PCT": "5.0",
    "MAX_MARKET_DAILY_LOSS_PCT": "3.0", "MARKET_COOLDOWN_MINUTES": "60", "MAX_MARGIN_UTILIZATION_PCT": "1000.0",
    "MAX_MARKET_MARGIN_UTILIZATION_PCT": "1000.0", "MAX_POSITIONS_PER_SECTOR": "1",
    "MARGIN_VERIFY": "off",           # the golden must not depend on live Upstox margins or on the operator's cached margin_rates.json
}


def _load(market: str, stem: str) -> pd.DataFrame:
    df = pd.read_csv(ARCHIVE_ROOT / market / f"{stem}_5minute.csv")
    df["_ts"] = pd.to_datetime(df["timestamp"])
    df["_day"] = df["_ts"].dt.strftime("%Y-%m-%d")
    return df


def run_replay(days=None, instruments=None) -> dict:
    """Returns {"orders": [...], "trades": [...], "alerts": [...]} for the whole replay."""
    from engine import live_dryrun
    from engine.database import TradingDB

    days = days or DAYS
    instruments = instruments or INSTRUMENTS
    data = {sym: _load(m, stem) for m, stem, sym in instruments}
    symbols = [sym for _, _, sym in instruments]
    sim = {"now": datetime(2026, 8, 26, 9, 0, tzinfo=IST), "bars": {}}   # bars: sym -> candles visible now
    alerts: list[dict] = []

    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return sim["now"] if tz is None else sim["now"].astimezone(tz)

    def fake_candles(broker, symbol, today):
        return list(sim["bars"].get(symbol, []))

    tmp = tempfile.mkdtemp()
    with ExitStack() as st:
        for k, v in ENV.items():
            st.enter_context(patch.dict(os.environ, {k: v}))
        from markets.commodity.scalping import backtest as _cbt
        from markets.currency.scalping import backtest as _ubt
        from markets.equity.scalping import entry_signal as _eq
        for table in (_cbt.ENTRY_THRESHOLDS, _ubt.ENTRY_THRESHOLDS):
            for key, row in table.items():
                st.enter_context(patch.dict(row, {k: v for k, v in PERMISSIVE.items() if k in row}))
        st.enter_context(patch.dict(_eq.ENTRY_THRESHOLDS, {k: v for k, v in PERMISSIVE.items() if k in _eq.ENTRY_THRESHOLDS}))
        st.enter_context(patch.object(live_dryrun, "datetime", SimDatetime))
        st.enter_context(patch.object(live_dryrun, "_fetch_candles", fake_candles))
        st.enter_context(patch.object(live_dryrun.DryRunner, "_maybe_sample_spread", lambda self, sym, now: None))
        import time
        from core import slippage
        st.enter_context(patch("core.slippage._resolve_csv", lambda: Path(tmp) / "no_spreads.csv"))
        st.enter_context(patch.object(slippage, "_cache", {}))
        st.enter_context(patch.object(slippage, "_cache_loaded_at", time.monotonic()))
        st.enter_context(patch.object(live_dryrun.telegram, "send", lambda *a, **k: None))
        st.enter_context(patch.object(live_dryrun.telegram, "alert_exit", lambda *a, **k: alerts.append({"exit": a[0], "reason": a[3], "net": round(a[4], 2)})))
        st.enter_context(patch.object(live_dryrun.telegram, "alert_entry", lambda sig, cap: alerts.append({"entry": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in sorted(sig.items()) if k not in ("time",)}})))

        db = TradingDB(str(Path(tmp) / "replay.db"))
        broker = MagicMock()
        runner = live_dryrun.DryRunner(broker=broker, db=db, symbols=symbols, capital=100000.0,
                                       risk_pct=4.0, leverage=5.0, account_id="REPLAY")
        for day in days:
            stamps = sorted({t for sym in symbols for t in data[sym].loc[data[sym]["_day"] == day, "_ts"]})
            for ts in stamps:
                sim["bars"] = {}
                for sym in symbols:
                    df = data[sym]
                    win = df[(df["_day"] == day) & (df["_ts"] <= ts)]
                    sim["bars"][sym] = win[["timestamp", "open", "high", "low", "close", "volume"]].to_dict("records")
                sim["now"] = ts.to_pydatetime().astimezone(IST) + timedelta(minutes=5, seconds=10)
                with contextlib.redirect_stdout(io.StringIO()):
                    runner.scan()

        import sqlite3
        con = sqlite3.connect(str(Path(tmp) / "replay.db")); con.row_factory = sqlite3.Row
        orders = [dict(r) for r in con.execute("SELECT order_id, symbol, direction, intent, order_type, quantity, requested_price, fill_price, status, tag FROM orders ORDER BY rowid")]
        trades = [dict(r) for r in con.execute("SELECT symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, exit_reason, gross_pnl, total_friction, net_pnl, capital_after, rsi, adx FROM trades ORDER BY trade_id")]
        con.close()
    return {"orders": orders, "trades": trades, "alerts": alerts}


if __name__ == "__main__":       # python -m tests.replay_harness quick|full OUT.json
    import json, sys
    quick = sys.argv[1] == "quick"
    out = run_replay(days=QUICK_DAYS, instruments=QUICK_INSTRUMENTS) if quick else run_replay()
    with open(sys.argv[2], "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True, default=str)
    print(f"{sys.argv[1]}: {len(out['orders'])} orders, {len(out['trades'])} trades -> {sys.argv[2]}")
