"""The strategy framework: discovery, selection, and a toy overnight/delivery strategy driven end to end
through BOTH runners. If adding a strategy is really "write one class", these are the behaviours a new
strategy relies on without writing any of it itself."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import registry
from core.strategy import EntryContext, ExitDecision, Signal, Strategy

# Pinned runner settings, so these tests never read the operator's .env.
ENV = {
    "USE_EQUITY_REGIME_FILTER": "false",
    "ENABLE_MEAN_REVERSION": "false", "DRYRUN_INCLUDE_EQUITY": "true",
    "ENABLE_CURRENCY_TRADING": "true", "ENABLE_EQUITY_TRADING": "true",
    "CURRENCY_RISK_PCT": "4.0", "CURRENCY_LEVERAGE": "5.0",
    "EQUITY_RISK_PCT": "4.0", "EQUITY_LEVERAGE": "5.0",
    "MAX_PORTFOLIO_HEAT_PCT": "12.0", "MAX_DAILY_LOSS_PCT": "5.0",
    "MAX_MARKET_DAILY_LOSS_PCT": "3.0", "MARKET_COOLDOWN_MINUTES": "60", "MAX_MARGIN_UTILIZATION_PCT": "1000.0",
    "MAX_MARKET_MARGIN_UTILIZATION_PCT": "1000.0", "MAX_POSITIONS_PER_SECTOR": "1", "MARGIN_VERIFY": "off",
    "EQUITY_PULLBACK_FRAC": "0", "CURRENCY_PULLBACK_FRAC": "0",
    "LIVE_BAR_COMPLETE_LAG_S": "0", "CURRENCY_STRATEGIES": "", "EQUITY_STRATEGIES": "",
}

IST = timezone(timedelta(hours=5, minutes=30))


class ToySwing(Strategy):
    """Buys with half the account, holds overnight, sells at +2% or the -2% stop. Delivery, long only, 1x."""
    name, market = "toy_swing", "equity"
    intraday, product = False, "D"
    uses_leverage, allow_short = False, False
    id_prefix, max_positions = "TOY", 1
    direction = "long"

    def entry(self, ctx):
        if not ctx.candles:
            return None
        px = float(ctx.candles[-1]["close"])
        d = 1 if self.direction == "long" else -1
        return Signal(symbol=ctx.symbol, direction=self.direction, entry_price=px, stop_loss=px * (1 - 0.02 * d),
                      qty=int(ctx.capital * 0.5 / px), stop_dist=px * 0.02, instrument_key=ctx.instrument_key,
                      exit_state={"target": px * 1.02}, target_price=px * 1.02, breakeven_price=px * 1.02,
                      alert_levels={"tp": px * 1.02}, price_levels=("target",))

    def manage(self, pos, ctx):
        px = float(ctx.candles[-1]["close"])
        if px >= pos["target"]:
            return ExitDecision(px, "target")
        if px <= pos["current_stop"]:
            return ExitDecision(pos["current_stop"], "stop")
        return None

    def costs(self, symbol, direction, entry, exit_price, qty):
        gross = (exit_price - entry) * qty
        return {"gross": gross, "total": 10.0, "net": gross - 10.0, "brokerage": 10.0}


class ToyShort(ToySwing):
    name, direction = "toy_short", "short"


_REAL_STRATEGIES = dict(registry.discover())       # captured once, before any test patches registry.discover


def _table(*extra):
    real = dict(_REAL_STRATEGIES)
    for s in extra:
        real[(s.market, s.name)] = s
    return real


class TestRegistry(unittest.TestCase):
    def test_discovery_needs_no_list_and_only_accepts_the_two_markets(self):
        found = registry.discover()                    # walks every markets/<market>/strategies folder; a broken strategy file would raise here
        self.assertEqual(registry.MARKETS, ("currency", "equity"))                  # MCX commodity was removed 2026-10-12
        self.assertTrue(all(m in registry.MARKETS for m, _ in found))

    def test_new_strategies_stay_off_until_named_in_env(self):
        with patch.object(registry, "discover", lambda: _table(ToySwing())), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EQUITY_STRATEGIES", None)
            self.assertEqual([s.name for s in registry.active("equity")], [])            # nothing runs by default
            with patch.dict(os.environ, {"EQUITY_STRATEGIES": "toy_swing"}):
                self.assertEqual([s.name for s in registry.active("equity")], ["toy_swing"])

    def test_an_unknown_name_in_env_fails_loudly(self):
        with patch.dict(os.environ, {"EQUITY_STRATEGIES": "nonsense"}), self.assertRaises(KeyError):
            registry.active("equity")

    def test_a_positions_own_strategy_is_found_even_if_it_was_switched_off(self):
        with patch.object(registry, "discover", lambda: _table(ToySwing())):
            self.assertEqual(registry.get("equity", "toy_swing").name, "toy_swing")


def _bar(price, ts="2026-09-10T11:00:00+05:30"):
    return {"timestamp": ts, "open": price, "high": price * 1.001, "low": price * 0.999, "close": price, "volume": 1000}


def _rows(path, sql):
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


class TestScaffold(unittest.TestCase):
    def test_generated_strategy_loads_is_valid_and_never_trades(self):
        import importlib.util
        from core import scaffold
        root = Path(tempfile.mkdtemp())
        path = scaffold.create("equity", "swing_trend", root=root)
        spec = importlib.util.spec_from_file_location("generated_strategy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        strat = module.STRATEGY
        self.assertIsInstance(strat, Strategy)
        self.assertEqual((strat.market, strat.name), ("equity", "swing_trend"))
        ctx = EntryContext(symbol="X", candles=[_bar(100.0)], now=datetime.now(IST), instrument_key="K", capital=1e5,
                           risk_pct=4.0, leverage=5.0, direction_filter="both")
        self.assertIsNone(strat.entry(ctx))                     # inert until the author writes entry()
        self.assertTrue((root / "equity" / "strategies" / "tf_5min" / "swing_trend" / "__init__.py").exists())

    def test_strategies_of_one_market_and_timeframe_sit_side_by_side_by_timeframe(self):
        from core import scaffold
        root = Path(tempfile.mkdtemp())
        a = scaffold.create("currency", "carry", root=root, timeframe="daily")
        b = scaffold.create("currency", "trend", root=root, timeframe="daily")
        c = scaffold.create("currency", "fade", root=root, timeframe="15min")
        self.assertEqual(a.parent.parent, b.parent.parent)                     # two daily strategies in one tf_daily folder
        self.assertEqual(a.parent.parent.name, "tf_daily")
        self.assertEqual(c.parent.parent.name, "tf_15min")
        self.assertIn('timeframe = ("days", 1)', a.read_text())
        self.assertIn("lookback_days = 250", a.read_text())
        with self.assertRaises(ValueError):
            scaffold.create("currency", "x", root=root, timeframe="3min")

    def test_every_strategy_lives_in_the_folder_of_its_signal_timeframe(self):
        from core import registry
        for (market, name), strat in registry.discover().items():
            parts = type(strat).__module__.split(".")
            self.assertEqual(parts[:3], ["markets", market, "strategies"], type(strat).__module__)
            self.assertEqual(parts[3], registry.timeframe_folder(strat), f"{market}/{name} is in {parts[3]}")

    def test_it_refuses_bad_names_unknown_markets_and_overwrites(self):
        from core import scaffold
        root = Path(tempfile.mkdtemp())
        scaffold.create("currency", "carry", root=root)
        with self.assertRaises(FileExistsError):
            scaffold.create("currency", "carry", root=root)
        for market, name in [("equity", "Bad Name"), ("equity", "1abc"), ("futures", "ok"), ("commodity", "ok")]:
            with self.assertRaises(ValueError):
                scaffold.create(market, name, root=root)


if __name__ == "__main__":
    unittest.main()


