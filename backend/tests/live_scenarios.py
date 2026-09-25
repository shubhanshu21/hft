"""Scripted real-order scenarios for LiveTrader, run against a fake broker.

Records exactly what would reach the broker (orders: side, quantity, product, tag), what is written to
the DB and what is alerted, for fixed entry / exit / failure scenarios. Run on the pre-refactor code to
freeze a golden file, then require the strategy-driven code to reproduce it: the real-order path is the
last place a silent behaviour change is acceptable.

The same script drives both APIs (old: _enter / _enter_equity / _exit / _exit_equity taking a signal dict;
new: _enter(strategy, Signal, ...) / _exit(decision)) so the recorded outputs are directly comparable.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Isolation: sizing and costs read Upstox's cached margin / lot / tick rates (engine/margin_rates.py). The goldens must never depend on whatever the
# running daemon last cached in var/cache/margin_rates.json, so this process reads an empty, throw-away cache unless a test injects its own.
import tempfile as _tempfile
from pathlib import Path as _Path
from engine import margin_rates as _margin_rates
_margin_rates.RATES_PATH = _Path(_tempfile.mkdtemp()) / "margin_rates.json"
_margin_rates._rates = {}
from engine import blocked_log as _blocked_log
_blocked_log.PATH = _Path(_tempfile.mkdtemp()) / "blocked_entries.jsonl"


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 10, 11, 30, tzinfo=IST)


class FakeBroker:
    dry_run = False

    def __init__(self, funds=1_000_000.0, status="complete", fill_offset=0.0, place_returns_id=True):
        self.calls, self.funds, self.status = [], funds, status
        self.fill_offset, self.place_returns_id, self._n = fill_offset, place_returns_id, 0
        self._last_price = None

    def get_available_funds(self):
        return self.funds

    def _place(self, side, ikey, qty, product, tag):
        self.calls.append({"side": side, "ikey": ikey, "qty": qty, "product": product, "tag": tag})
        if not self.place_returns_id:
            return None
        self._n += 1
        return f"ORD{self._n}"

    def place_buy_order(self, ikey, qty, product="I", tag=""):
        return self._place("BUY", ikey, qty, product, tag)

    def place_sell_order(self, ikey, qty, product="I", tag=""):
        return self._place("SELL", ikey, qty, product, tag)

    def get_order_status(self, order_id):
        return self.status

    def get_fill_price(self, order_id):
        return self._last_price + self.fill_offset if self._last_price is not None else None

    def cancel_order(self, order_id):
        self.calls.append({"cancel": order_id})

    def __getattr__(self, name):          # anything else (candles, positions, ...) is unused here
        return MagicMock(return_value=None)


def _trader(broker, db_path):
    from engine import live_trading
    from engine.database import TradingDB
    env = {"USE_COMMODITY_REGIME_FILTER": "false", "USE_EQUITY_REGIME_FILTER": "false", "ENABLE_MEAN_REVERSION": "false"}
    with patch.dict(os.environ, env), patch.object(live_trading, "_build_symbol_map", lambda: {}):
        return live_trading.LiveTrader(broker=broker, db=TradingDB(db_path), symbols=["CRUDEOILM"], capital=100000.0,
                                       risk_pct=4.0, leverage=5.0, account_id="SCEN")


# ---- entry scenarios: (name, market, symbol, direction, entry, sl, extra levels, qty/lots, stop_dist, broker kwargs)
ENTRIES = [
    ("mcx_long_fill_at_assumed", "commodity", "CRUDEOILM", "long", 8600.0, 8570.0, {"tp": 8660.0, "be": 8620.0}, 5, 30.0, {}),
    ("mcx_short_slippage", "commodity", "GOLDM", "short", 150000.0, 150600.0, {"tp": 148800.0, "be": 149600.0}, 3, 600.0, {"fill_offset": 250.0}),
    ("ncd_long", "currency", "USDINR", "long", 95.75, 95.65, {"tp": 96.05, "be": 95.81}, 4, 0.10, {"fill_offset": 0.02}),
    ("eq_long", "equity", "TATASTEEL", "long", 189.9, 188.95, {"activation_price": 190.6, "trail_mult": 0.4}, 2500, 0.95, {}),
    ("eq_short_slippage", "equity", "SBIN", "short", 800.0, 804.0, {"activation_price": 797.0, "trail_mult": 0.3}, 300, 4.0, {"fill_offset": -1.5}),
    ("funds_insufficient", "commodity", "CRUDEOILM", "long", 8600.0, 8570.0, {"tp": 8660.0, "be": 8620.0}, 5, 30.0, {"funds": 1000.0}),
    ("order_rejected", "commodity", "CRUDEOILM", "long", 8600.0, 8570.0, {"tp": 8660.0, "be": 8620.0}, 5, 30.0, {"status": "rejected"}),
    ("order_ambiguous", "equity", "TATASTEEL", "long", 189.9, 188.95, {"activation_price": 190.6, "trail_mult": 0.4}, 2500, 0.95, {"status": "open"}),
    ("no_order_id", "commodity", "CRUDEOILM", "long", 8600.0, 8570.0, {"tp": 8660.0, "be": 8620.0}, 5, 30.0, {"place_returns_id": False}),
]

# ---- exit scenarios: (name, market, symbol, direction, position dict extras, reason, exit price, broker kwargs)
EXITS = [
    ("mcx_take_profit", "commodity", "CRUDEOILM", "long", {"tp": 8660.0, "be": 8620.0, "armed_be": True, "current_stop": 8605.0}, "take_profit", 8660.0, {}),
    ("mcx_stop", "commodity", "CRUDEOILM", "long", {"tp": 8660.0, "be": 8620.0, "armed_be": False, "current_stop": 8570.0}, "initial_stop", 8570.0, {}),
    ("ncd_short_be_stop", "currency", "USDINR", "short", {"tp": 95.4, "be": 95.7, "armed_be": True, "current_stop": 95.74}, "be_stop", 95.74, {"fill_offset": 0.01}),
    ("eq_trail_stop", "equity", "TATASTEEL", "long", {"armed_trail": True, "activation_price": 190.6, "trail_mult": 0.4, "current_stop": 190.2}, "trail_stop", 190.2, {}),
    ("eq_short_initial_stop", "equity", "SBIN", "short", {"armed_trail": False, "activation_price": 797.0, "trail_mult": 0.3, "current_stop": 804.0}, "initial_stop", 804.0, {}),
    ("eq_exit_rejected", "equity", "TATASTEEL", "long", {"armed_trail": False, "activation_price": 190.6, "trail_mult": 0.4, "current_stop": 188.95}, "initial_stop", 188.95, {"status": "rejected"}),
    ("mcx_exit_ambiguous", "commodity", "CRUDEOILM", "long", {"tp": 8660.0, "be": 8620.0, "armed_be": False, "current_stop": 8570.0}, "initial_stop", 8570.0, {"status": "open"}),
]


def _entry_via_new_api(trader, broker, market, sym, direction, entry, sl, extra, qty, sdist):
    from core import registry
    from core.strategy import Signal
    strat = registry.get(market, "scalping")
    lot = strat.lot_size(sym)
    if market == "equity":
        sig = Signal(symbol=sym, direction=direction, entry_price=entry, stop_loss=sl, qty=qty, stop_dist=sdist,
                     instrument_key=f"IK|{sym}", lot_size=1, exit_state={"armed_trail": False, **extra},
                     target_price=extra["activation_price"], breakeven_price=extra["activation_price"],
                     price_levels=("activation_price",))
    else:
        sig = Signal(symbol=sym, direction=direction, entry_price=entry, stop_loss=sl, qty=qty, stop_dist=sdist,
                     instrument_key=f"IK|{sym}", lot_size=lot, exit_state={**extra, "armed_be": False},
                     target_price=extra["tp"], breakeven_price=extra["be"], price_levels=("tp", "be"))
    lev = 5.0 if sym != "GBPINR" else 3.5
    return trader._enter(strat, sig, lev, NOW)


def _entry_via_old_api(trader, broker, market, sym, direction, entry, sl, extra, qty, sdist):
    sig = {"symbol": sym, "direction": direction, "entry_price": entry, "sl": sl, "stop_dist": sdist,
           "instrument_key": f"IK|{sym}", "lots": qty, "qty": qty, **extra}
    if market == "equity":
        sig["activation_price"] = extra["activation_price"]
        return trader._enter_equity(sig, NOW)
    return trader._enter(sig, NOW)


def run() -> dict:
    from engine import live_trading
    new_api = hasattr(live_trading.LiveTrader, "_try_enter")
    out = {"entries": {}, "exits": {}}
    sent: list[str] = []
    import time
    from core import slippage
    with patch.object(slippage, "_cache", {}), patch.object(slippage, "_cache_loaded_at", time.monotonic()), \
         patch.object(live_trading.telegram, "send", lambda m, *a, **k: sent.append(m)), \
         patch.object(live_trading, "ORDER_FILL_TIMEOUT_SEC", 0.02), patch.object(live_trading, "ORDER_CANCEL_RECHECK_SEC", 0.02), \
         patch.object(live_trading, "ORDER_POLL_INTERVAL_SEC", 0.005):
        for name, market, sym, direction, entry, sl, extra, qty, sdist, bk in ENTRIES:
            sent.clear()
            tmp = Path(tempfile.mkdtemp()) / "s.db"
            broker = FakeBroker(**bk); broker._last_price = entry
            trader = _trader(broker, str(tmp))
            (_entry_via_new_api if new_api else _entry_via_old_api)(trader, broker, market, sym, direction, entry, sl, extra, qty, sdist)
            con = sqlite3.connect(str(tmp)); con.row_factory = sqlite3.Row
            pos = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in trader.positions.get(sym, {}).items()
                   if k in ("direction", "qty", "entry_price", "current_stop", "tp", "be", "activation_price", "trail_mult", "armed_be", "armed_trail", "best_price", "stop_dist", "instrument_key")}
            out["entries"][name] = {
                "broker_calls": broker.calls, "position": pos,
                "db_position": [{k: r[k] for k in ("symbol", "direction", "qty", "entry_price", "current_stop", "target_price", "breakeven_price")} for r in con.execute("SELECT * FROM positions")],
                "db_orders": [{k: r[k] for k in ("symbol", "direction", "intent", "quantity", "requested_price", "fill_price", "status", "tag")} for r in con.execute("SELECT * FROM orders")],
                "alerts": list(sent),
            }
            con.close()
        for name, market, sym, direction, extra, reason, price, bk in EXITS:
            sent.clear()
            tmp = Path(tempfile.mkdtemp()) / "s.db"
            broker = FakeBroker(**bk); broker._last_price = price
            trader = _trader(broker, str(tmp))
            lot = 1 if market == "equity" else (1000 if market == "currency" else {"CRUDEOILM": 10, "GOLDM": 10}.get(sym, 1))
            qty = 2500 if market == "equity" else 5
            entry = {"CRUDEOILM": 8600.0, "USDINR": 95.75, "TATASTEEL": 189.9, "SBIN": 800.0}[sym]
            trader.db.open_position(position_id="POS1", symbol=sym, direction=direction, qty=qty, entry_price=entry,
                                    current_stop=extra["current_stop"], target_price=extra.get("tp", extra.get("activation_price")),
                                    breakeven_price=extra.get("be", extra.get("activation_price")), account_id="SCEN")
            pos = {"position_id": "POS1", "direction": direction, "qty": qty, "entry_price": entry, "best_price": entry,
                   "entry_time": NOW - timedelta(minutes=20), "stop_dist": abs(entry - extra["current_stop"]) or 1.0,
                   "instrument_key": f"IK|{sym}", "symbol": sym, "strategy": "scalping", **extra}
            trader.positions[sym] = pos
            if new_api:
                from core.strategy import ExitDecision
                trader._exit(sym, pos, ExitDecision(price, reason), NOW)
            else:
                (trader._exit_equity if market == "equity" else trader._exit)(sym, pos, reason, NOW)
            con = sqlite3.connect(str(tmp)); con.row_factory = sqlite3.Row
            out["exits"][name] = {
                "broker_calls": broker.calls, "still_open": sym in trader.positions, "capital": round(trader.capital, 4),
                "db_orders": [{k: r[k] for k in ("symbol", "direction", "intent", "quantity", "requested_price", "fill_price", "status", "tag")} for r in con.execute("SELECT * FROM orders")],
                "db_trades": [{k: (round(r[k], 4) if isinstance(r[k], float) else r[k]) for k in ("symbol", "direction", "qty", "entry_price", "exit_price", "exit_reason", "gross_pnl", "total_friction", "net_pnl")} for r in con.execute("SELECT * FROM trades")],
                "alerts": list(sent),
            }
            con.close()
    return out


if __name__ == "__main__":
    import json, sys
    out = run()
    with open(sys.argv[1], "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True, default=str)
    print(f"{len(out['entries'])} entry + {len(out['exits'])} exit scenarios -> {sys.argv[1]}")
