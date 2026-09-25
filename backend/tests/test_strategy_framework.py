"""The strategy framework: discovery, selection, and a toy overnight/delivery strategy driven end to end
through BOTH runners. If adding a strategy is really "write one class", these are the behaviours a new
strategy relies on without writing any of it itself."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import registry
from core.strategy import EntryContext, ExitDecision, Signal, Strategy
from tests.replay_harness import ENV

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
    def test_the_three_existing_scalpers_are_discovered_without_any_list(self):
        found = set(registry.discover())
        self.assertTrue({("commodity", "scalping"), ("currency", "scalping"), ("equity", "scalping")} <= found)

    def test_new_strategies_stay_off_until_named_in_env(self):
        with patch.object(registry, "discover", lambda: _table(ToySwing())), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EQUITY_STRATEGIES", None)
            self.assertEqual([s.name for s in registry.active("equity")], ["scalping"])
            with patch.dict(os.environ, {"EQUITY_STRATEGIES": "scalping, toy_swing"}):
                self.assertEqual([s.name for s in registry.active("equity")], ["scalping", "toy_swing"])
            with patch.dict(os.environ, {"EQUITY_STRATEGIES": "toy_swing"}):
                self.assertEqual([s.name for s in registry.active("equity")], ["toy_swing"])

    def test_an_unknown_name_in_env_fails_loudly(self):
        with patch.dict(os.environ, {"EQUITY_STRATEGIES": "nonsense"}), self.assertRaises(KeyError):
            registry.active("equity")

    def test_a_positions_own_strategy_is_found_even_if_it_was_switched_off(self):
        with patch.object(registry, "discover", lambda: _table(ToySwing())):
            self.assertEqual(registry.get("equity", "toy_swing").name, "toy_swing")


@contextmanager
def _dry_runner(*strategies, symbols=("TATASTEEL",), db_path=None, clock=None, candles=None):
    from engine import live_dryrun
    from engine.database import TradingDB
    sim = clock if clock is not None else {"now": datetime(2026, 9, 10, 11, 0, tzinfo=IST)}
    bars = candles if candles is not None else {}
    alerts = []

    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return sim["now"] if tz is None else sim["now"].astimezone(tz)

    with ExitStack() as st:
        st.enter_context(patch.object(registry, "discover", lambda: _table(*strategies)))
        for k, v in {**ENV, "EQUITY_STRATEGIES": ",".join(s.name for s in strategies) or "scalping"}.items():
            st.enter_context(patch.dict(os.environ, {k: v}))
        st.enter_context(patch.object(live_dryrun, "datetime", SimDatetime))
        st.enter_context(patch.object(live_dryrun, "_fetch_candles", lambda b, s, t: list(bars.get(s, []))))
        st.enter_context(patch.object(live_dryrun.DryRunner, "_maybe_sample_spread", lambda self, sym, now: None))
        st.enter_context(patch("core.slippage._resolve_csv", lambda: Path(tempfile.gettempdir()) / "no_spreads_x.csv"))
        st.enter_context(patch.object(live_dryrun.telegram, "send", lambda *a, **k: None))
        st.enter_context(patch.object(live_dryrun.telegram, "alert_exit", lambda *a, **k: None))
        st.enter_context(patch.object(live_dryrun.telegram, "alert_entry", lambda sig, cap: alerts.append(sig)))
        path = db_path or str(Path(tempfile.mkdtemp()) / "fw.db")

        def make():
            return live_dryrun.DryRunner(broker=MagicMock(), db=TradingDB(path), symbols=list(symbols), capital=100000.0,
                                         risk_pct=4.0, leverage=5.0, account_id="FW")
        yield make, sim, bars, alerts, path


def _bar(price, ts="2026-09-10T11:00:00+05:30"):
    return {"timestamp": ts, "open": price, "high": price * 1.001, "low": price * 0.999, "close": price, "volume": 1000}


def _rows(path, sql):
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


class TestOvernightStrategyInDryRunner(unittest.TestCase):
    def test_entry_is_tagged_sized_at_1x_and_persisted_with_its_state(self):
        with _dry_runner(ToySwing()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            signals = runner.scan()
            self.assertEqual(len(signals), 1)
            pos = runner.positions["TATASTEEL"]
            self.assertEqual(pos["strategy"], "toy_swing")
            self.assertEqual(pos["leverage"], 1.0)                              # uses_leverage = False
            self.assertAlmostEqual(pos["margin_used"], pos["qty"] * 200.0, places=1)   # full notional, no leverage
            row = _rows(path, "SELECT strategy, state FROM positions")[0]
            self.assertEqual(row["strategy"], "toy_swing")
            self.assertIn('"target"', row["state"])
            self.assertEqual(signals[0]["strategy"], "toy_swing")      # what the alert is built from

    def test_it_is_not_squared_off_at_the_intraday_close_and_survives_a_restart(self):
        with _dry_runner(ToySwing()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            runner.scan()
            sim["now"] = datetime(2026, 9, 10, 15, 20, tzinfo=IST)               # past the scalpers' 15:15 EOD exit
            bars["TATASTEEL"] = [_bar(200.5)]
            runner.scan()
            self.assertIn("TATASTEEL", runner.positions)                          # still open: nothing forced it out
            restarted = make()                                                    # new process, same database
            pos = restarted.positions["TATASTEEL"]
            self.assertEqual(pos["strategy"], "toy_swing")
            self.assertAlmostEqual(pos["target"], 204.0)                          # exit state came back from the DB
            self.assertEqual(pos["margin_used"], runner.positions["TATASTEEL"]["margin_used"])

    def test_it_exits_by_its_own_rule_with_its_own_costs_and_is_recorded_under_its_name(self):
        with _dry_runner(ToySwing()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            runner.scan()
            sim["now"] = datetime(2026, 9, 11, 11, 5, tzinfo=IST)                # the next day
            bars["TATASTEEL"] = [_bar(205.0)]
            runner.scan()
            self.assertNotIn("TATASTEEL", runner.positions)
            trade = _rows(path, "SELECT strategy, exit_reason, total_friction FROM trades")[0]
            self.assertEqual((trade["strategy"], trade["exit_reason"], trade["total_friction"]), ("toy_swing", "target", 10.0))

    def test_the_runner_records_the_entry_bar_and_it_survives_a_restart(self):
        # core/exits._side needs this to ignore prices from before the entry (see tests/test_exit_entry_bar.py)
        with _dry_runner(ToySwing()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0, ts="2026-09-10T10:55:00+05:30"), _bar(201.0, ts="2026-09-10T11:00:00+05:30")]
            runner = make()
            runner.scan()
            self.assertEqual(runner.positions["TATASTEEL"]["entry_bar_ts"], "2026-09-10T11:00:00+05:30")
            self.assertIn("entry_bar_ts", _rows(path, "SELECT state FROM positions")[0]["state"])
            self.assertEqual(make().positions["TATASTEEL"]["entry_bar_ts"], "2026-09-10T11:00:00+05:30")

    def test_a_long_only_strategy_can_never_open_a_short(self):
        with _dry_runner(ToyShort()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            self.assertEqual(runner.scan(), [])
            self.assertEqual(runner.positions, {})

    def test_max_positions_caps_that_strategy_only(self):
        with _dry_runner(ToySwing(), symbols=("TATASTEEL", "SBIN")) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"], bars["SBIN"] = [_bar(200.0)], [_bar(300.0)]
            runner = make()
            runner.scan()
            self.assertEqual(sorted(runner.positions), ["TATASTEEL"])             # max_positions = 1

    def test_only_one_position_per_symbol_across_strategies(self):
        with _dry_runner(ToySwing(), symbols=("TATASTEEL",)) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            runner.strategies["equity"] = [ToySwing(), registry.get("equity", "scalping")]
            runner.scan()
            self.assertEqual(len(runner.positions), 1)
            self.assertEqual(runner.positions["TATASTEEL"]["strategy"], "toy_swing")


class TestOvernightStrategyInLiveTrader(unittest.TestCase):
    def _trader(self, **broker_kw):
        from tests.live_scenarios import FakeBroker, _trader
        broker = FakeBroker(**broker_kw)
        return broker, _trader(broker, str(Path(tempfile.mkdtemp()) / "l.db"))

    def _signal(self):
        return ToySwing().entry(EntryContext(symbol="TATASTEEL", candles=[_bar(200.0)], now=datetime.now(IST), instrument_key="IK|T",
                                             capital=100000.0, risk_pct=4.0, leverage=1.0, direction_filter="both", full_session=True))

    def test_real_orders_use_the_delivery_product_and_are_funds_checked_at_1x(self):
        broker, trader = self._trader()
        broker._last_price = 200.0
        signal = self._signal()
        with patch("engine.live_trading.telegram.send"):
            self.assertTrue(trader._enter(ToySwing(), signal, 1.0, datetime(2026, 9, 10, 11, 0, tzinfo=IST)))
        self.assertEqual(broker.calls[0]["product"], "D")
        self.assertEqual(trader.positions["TATASTEEL"]["strategy"], "toy_swing")

    def test_the_live_runner_also_records_the_entry_bar(self):
        broker, trader = self._trader()
        broker._last_price = 200.0
        trader.strategies["equity"] = [ToySwing()]
        trader.candle_source = None
        candles = [_bar(200.0, ts="2026-09-10T11:00:00+05:30")]
        with patch("engine.live_trading._fetch_candles", lambda *a, **k: list(candles)), \
             patch.object(type(trader), "_entry_gate_rejection", lambda *a, **k: None), \
             patch("engine.live_trading.telegram.send"):
            trader.symbol_map["TATASTEEL"] = "IK|T"
            self.assertTrue(trader._try_enter(ToySwing(), "TATASTEEL", datetime(2026, 9, 10, 11, 0, 20, tzinfo=IST)))
        self.assertEqual(trader.positions["TATASTEEL"]["entry_bar_ts"], "2026-09-10T11:00:00+05:30")

    def test_a_delivery_order_needs_the_whole_notional_in_funds_not_a_fifth_of_it(self):
        signal = self._signal()
        notional = signal.qty * 200.0
        broker, trader = self._trader(funds=notional / 5 * 1.1)                  # enough for 5x intraday margin, not for delivery
        broker._last_price = 200.0
        with patch("engine.live_trading.telegram.send") as send:
            self.assertFalse(trader._enter(ToySwing(), signal, 1.0, datetime(2026, 9, 10, 11, 0, tzinfo=IST)))
        self.assertEqual(broker.calls, [])                                        # refused before any order
        self.assertIn("INSUFFICIENT FUNDS", send.call_args[0][0])


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
                           risk_pct=4.0, leverage=5.0, direction_filter="both", full_session=True)
        self.assertIsNone(strat.entry(ctx))                     # inert until the author writes entry()
        self.assertTrue((root / "equity" / "swing_trend" / "__init__.py").exists())

    def test_it_refuses_bad_names_unknown_markets_and_overwrites(self):
        from core import scaffold
        root = Path(tempfile.mkdtemp())
        scaffold.create("commodity", "carry", root=root)
        with self.assertRaises(FileExistsError):
            scaffold.create("commodity", "carry", root=root)
        for market, name in [("equity", "Bad Name"), ("equity", "1abc"), ("futures", "ok")]:
            with self.assertRaises(ValueError):
                scaffold.create(market, name, root=root)


if __name__ == "__main__":
    unittest.main()


class TestPullbackEntryInDryRunner(unittest.TestCase):
    """core/entry_pullback wired into the runner: off by default, and when on a signal rests as a limit and opens only on a pullback, at the limit price."""

    def _env(self, on):
        env = {"EQUITY_PULLBACK_FRAC": "0.25", "EQUITY_PULLBACK_THROUGH": "0", "EQUITY_PULLBACK_BARS": "3"} if on else {}
        return patch.dict(os.environ, env)

    def test_off_by_default_enters_at_the_signal(self):
        with patch.dict(os.environ, {"EQUITY_PULLBACK_FRAC": "0"}), _dry_runner(ToySwing()) as (make, sim, bars, alerts, path):
            bars["TATASTEEL"] = [_bar(200.0)]
            runner = make()
            self.assertEqual(len(runner.scan()), 1)
            self.assertEqual(runner.positions["TATASTEEL"]["entry_price"], 200.0)

    def test_signal_rests_then_fills_on_the_pullback_and_the_levels_move_with_it(self):
        with _dry_runner(ToySwing()) as (make, sim, bars, alerts, path), self._env(True):      # the runner fixture pins pullback off; switch it on after it
            bars["TATASTEEL"] = [_bar(200.0, "2026-09-10T10:55:00+05:30")]
            runner = make()
            self.assertEqual(runner.scan(), [])                                       # signal -> resting limit, no position
            self.assertNotIn("TATASTEEL", runner.positions)
            self.assertIn("TATASTEEL", runner.pending_entries)
            limit = 200.0 - 0.25 * 200.0 * 0.02                                        # 0.25 of the 4.0 stop distance = 199.0
            sim["now"] = datetime(2026, 9, 10, 11, 2, tzinfo=IST)
            bars["TATASTEEL"] = [_bar(200.0, "2026-09-10T10:55:00+05:30"), {**_bar(200.0, "2026-09-10T11:00:00+05:30"), "low": 199.5, "high": 200.4}]
            self.assertEqual(runner.scan(), [])                                       # not pulled back enough yet
            sim["now"] = datetime(2026, 9, 10, 11, 7, tzinfo=IST)
            bars["TATASTEEL"] = [_bar(200.0, "2026-09-10T10:55:00+05:30"), {**_bar(200.0, "2026-09-10T11:00:00+05:30"), "low": 199.5, "high": 200.4},
                                 {**_bar(199.4, "2026-09-10T11:05:00+05:30"), "low": 198.9, "high": 199.8}]
            self.assertEqual(len(runner.scan()), 1)                                    # filled
            pos = runner.positions["TATASTEEL"]
            self.assertAlmostEqual(pos["entry_price"], limit, places=2)
            self.assertAlmostEqual(pos["current_stop"], limit - 4.0, places=2)         # stop distance unchanged, anchored to the fill
            self.assertAlmostEqual(pos["target"], 204.0 - 1.0, places=2)               # the strategy's own price level moved by the same 1.0
            self.assertNotIn("TATASTEEL", runner.pending_entries)
