"""Reliability and safety nets: the live-trading kill switch, broker request timeouts and response parsing, heartbeat + watchdog."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine import heartbeat, safety_gate, watchdog

IST = heartbeat.IST


class TestSafetyGate(unittest.TestCase):
    """Real orders need ALL THREE independent gates. Any other combination must force paper mode."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".live_trading_armed"
        for target, value in ((safety_gate, "ARMED_STATE_FILE"), (safety_gate, "ARMED_STATE_DIR")):
            p = patch.object(target, value, self.state if value == "ARMED_STATE_FILE" else self.tmp)
            p.start()
            self.addCleanup(p.stop)

    def _arm(self, *components, phrase=None):
        self.state.write_text((phrase or safety_gate.CONFIRM_PHRASE) + "\n" + "\n".join(components) + "\n")

    def _allowed(self, kill=False, env="true", armed=("upstox",), component="upstox", phrase=None):
        self.state.unlink(missing_ok=True)
        if armed:
            self._arm(*armed, phrase=phrase)
        with patch.object(safety_gate, "KILL_SWITCH_ENGAGED", kill), patch.dict(os.environ, {"ALLOW_LIVE_TRADING": env}):
            return safety_gate.live_trading_allowed(component)

    def test_the_shipped_kill_switch_is_engaged(self):
        self.assertTrue(safety_gate.KILL_SWITCH_ENGAGED)          # flipping this is a deliberate code change, never a side effect

    def test_all_three_gates_open_allows_live(self):
        self.assertTrue(self._allowed())

    def test_each_single_gate_closed_blocks(self):
        self.assertFalse(self._allowed(kill=True))                               # source-code kill switch
        self.assertFalse(self._allowed(env="false"))                             # .env flag off
        self.assertFalse(self._allowed(env="yes-please"))                        # anything that is not 1/true/yes
        self.assertFalse(self._allowed(armed=()))                                # never armed (no state file)
        self.assertFalse(self._allowed(phrase="i understand"))                   # wrong confirmation phrase
        self.assertFalse(self._allowed(armed=("binance",)))                      # armed for a different component
        self.assertTrue(self._allowed(armed=("ALL",)))                           # ...but ALL covers it

    def test_a_corrupt_state_file_fails_safe(self):
        self.state.write_bytes(b"\xff\xfe\x00garbage")
        with patch.object(safety_gate, "KILL_SWITCH_ENGAGED", False), patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true"}):
            self.assertFalse(safety_gate.live_trading_allowed("upstox"))

    def test_enforce_dry_run_never_loosens_a_paper_request(self):
        self.assertTrue(safety_gate.enforce_dry_run("upstox", True))
        with patch.object(safety_gate, "KILL_SWITCH_ENGAGED", False), patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true"}):
            self._arm("upstox")
            self.assertTrue(safety_gate.enforce_dry_run("upstox", True))          # asked for paper: stays paper even with every gate open
            self.assertFalse(safety_gate.enforce_dry_run("upstox", False))        # asked for live AND gates open: honoured

    def test_asking_for_live_with_a_gate_closed_is_forced_to_paper(self):
        self.assertTrue(safety_gate.enforce_dry_run("upstox", False))             # shipped defaults

    def test_arm_requires_the_exact_phrase_and_disarm_clears_it(self):
        self.assertFalse(safety_gate.arm("upstox", "yes"))
        self.assertFalse(self.state.exists())
        self.assertTrue(safety_gate.arm("upstox", safety_gate.CONFIRM_PHRASE))
        self.assertTrue(safety_gate.is_armed("upstox"))
        safety_gate.disarm("upstox")
        self.assertFalse(safety_gate.is_armed("upstox"))

    def test_the_broker_is_paper_whatever_the_caller_asks_for(self):
        from services.broker.upstox_broker import UpstoxBroker
        self.assertTrue(UpstoxBroker(access_token="x", dry_run=False).dry_run)


class TestBrokerTimeoutAndParsing(unittest.TestCase):
    def test_every_sdk_request_gets_a_default_timeout_and_a_callers_timeout_wins(self):
        import upstox_client
        from services.broker.upstox_broker import REQUEST_TIMEOUT_S, TimeoutApiClient
        seen = {}
        with patch.object(upstox_client.ApiClient, "call_api", lambda self, *a, **k: seen.update(k)):
            client = TimeoutApiClient(upstox_client.Configuration())
            client.call_api("/x", "GET")
            self.assertEqual(seen["_request_timeout"], REQUEST_TIMEOUT_S)
            client.call_api("/x", "GET", _request_timeout=3)
            self.assertEqual(seen["_request_timeout"], 3)
        self.assertTrue(all(t > 0 for t in REQUEST_TIMEOUT_S))

    def _broker(self, **apis):
        from services.broker.upstox_broker import UpstoxBroker
        b = UpstoxBroker(access_token="x", dry_run=True)
        for name, api in apis.items():
            setattr(b, name, api)
        return b

    def test_intraday_candles_are_parsed_into_dicts(self):
        rows = [["2026-09-24T09:15:00+05:30", 100, 101, 99, 100.5, 1200], ["2026-09-24T09:20:00+05:30", 100.5, 102, 100, 101, 900]]
        api = SimpleNamespace(get_intra_day_candle_data=lambda k, u, i: SimpleNamespace(status="success", data=SimpleNamespace(candles=rows)))
        out = self._broker(_history_v3_api=api).get_intraday_candles("MCX_FO|1", "minutes", 5)
        self.assertEqual(out[0], {"timestamp": rows[0][0], "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1200})
        self.assertEqual(len(out), 2)

    def test_candle_failures_return_none_instead_of_raising(self):
        def boom(*a):
            raise TimeoutError("read timed out")
        self.assertIsNone(self._broker(_history_v3_api=SimpleNamespace(get_intra_day_candle_data=boom)).get_intraday_candles("K", "minutes", 5))
        empty = SimpleNamespace(get_intra_day_candle_data=lambda *a: SimpleNamespace(status="error", data=None))
        self.assertIsNone(self._broker(_history_v3_api=empty).get_intraday_candles("K", "minutes", 5))

    def test_broker_positions_omit_flat_instruments_and_failure_is_not_flat(self):
        pos = [SimpleNamespace(instrument_token="A", quantity=5), SimpleNamespace(instrument_token="B", quantity=0),
               SimpleNamespace(instrument_token="C", quantity=-2)]
        api = SimpleNamespace(get_positions=lambda api_version: SimpleNamespace(data=pos))
        self.assertEqual(self._broker(_portfolio_api=api).get_broker_positions(), {"A": 5, "C": -2})
        def boom(api_version):
            raise TimeoutError()
        self.assertIsNone(self._broker(_portfolio_api=SimpleNamespace(get_positions=boom)).get_broker_positions())   # None = "could not check"


class TestHeartbeatAndWatchdog(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 12, 0, tzinfo=IST)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hb_path, self.state = self.tmp / "hb.json", self.tmp / "wd.json"
        self.sent, self.calls = [], []

    def _run(self, active=True):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return SimpleNamespace(stdout="active\n" if active else "inactive\n", returncode=0)
        return run

    def _check(self, hb, active=True, **kw):
        return watchdog.check(self.NOW, hb=hb, run=self._run(active), notify=self.sent.append, state_path=self.state, **kw)

    def _hb(self, phase="scan", late_s=0, next_in=0):
        """A heartbeat written (next_in + grace + late_s) seconds ago, i.e. late_s past its own deadline."""
        written = self.NOW - timedelta(seconds=next_in + heartbeat.GRACE_S + late_s)
        heartbeat.beat(phase, next_in, now=written, path=self.hb_path)
        return heartbeat.read(self.hb_path)

    def test_a_fresh_scan_beat_is_healthy(self):
        self.assertEqual(self._check(self._hb(late_s=-60)), "ok")
        self.assertEqual(self.sent, [])

    def test_a_long_overnight_sleep_is_not_a_hang(self):
        hb = self._hb("waiting_for_open", next_in=8 * 3600, late_s=-30)     # 8h sleep, still inside its promised window
        self.assertEqual(self._check(hb), "ok")

    def test_a_missed_deadline_on_a_running_service_restarts_it_and_alerts(self):
        self.assertEqual(self._check(self._hb(late_s=120)), "stale-restarted")
        self.assertIn(["systemctl", "--user", "restart", watchdog.SERVICE], self.calls)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("WATCHDOG RESTART", self.sent[0])

    def test_a_stopped_service_is_never_restarted_by_the_watchdog(self):
        self.assertEqual(self._check(self._hb(late_s=600), active=False), "not-active")
        self.assertFalse([c for c in self.calls if "restart" in c])

    def test_a_clean_stop_is_not_a_hang(self):
        self.assertEqual(self._check(self._hb("stopped", late_s=9999)), "ok")

    def test_no_heartbeat_file_yet_never_triggers_a_restart(self):
        with patch.object(heartbeat, "read", return_value=None):                # the real file exists whenever the daemon is running
            self.assertEqual(watchdog.check(self.NOW, hb=None, run=self._run(), notify=self.sent.append, state_path=self.state), "no-heartbeat")
        self.assertEqual(self.calls, [])

    def test_restarts_are_rate_limited_so_a_boot_hang_cannot_loop(self):
        hb = self._hb(late_s=120)
        self.assertEqual(self._check(hb), "stale-restarted")
        self.calls.clear()
        self.assertEqual(self._check(hb), "stale-alerted")             # same incident a moment later: alert, do not restart again
        self.assertFalse([c for c in self.calls if "restart" in c])

    def test_autorestart_can_be_switched_off(self):
        self.assertEqual(self._check(self._hb(late_s=120), autorestart=False), "stale-alerted")
        self.assertFalse([c for c in self.calls if "restart" in c])

    def test_beat_is_atomic_json_and_never_raises_on_a_bad_path(self):
        heartbeat.beat("scan", 30, now=self.NOW, path=self.hb_path)
        data = json.loads(self.hb_path.read_text())
        self.assertEqual(data["phase"], "scan")
        heartbeat.beat("scan", 0, path=self.tmp / "no" / "such" / "dir" / "hb.json")      # must not raise: a full disk cannot stop trading


if __name__ == "__main__":
    unittest.main()


class TestTokenLogic(unittest.TestCase):
    def _jwt(self, claims):
        import base64
        seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"

    def test_expiry_is_read_from_the_jwt_and_garbage_gives_none(self):
        from services.auth.upstox_auto_login import token_expiry_epoch
        self.assertEqual(token_expiry_epoch(self._jwt({"exp": 1790000000})), 1790000000.0)
        for bad in ("", "not-a-jwt", "a.b.c", self._jwt({"no_exp": 1})):
            self.assertIsNone(token_expiry_epoch(bad))          # None means "fall back to the periodic check", never "never expires"

    def test_a_valid_token_is_kept_and_no_login_is_attempted(self):
        from services.auth import upstox_auto_login as m
        with patch.object(m.UpstoxConfig, "auto_login_configured", return_value=True), patch.object(m.UpstoxConfig, "ACCESS_TOKEN", "tok"), \
                patch.object(m, "_token_is_valid", return_value=True), patch.object(m, "_auto_login_get_code") as login:
            self.assertEqual(m.ensure_fresh_upstox_token(), "tok")
            login.assert_not_called()

    def test_an_invalid_token_triggers_one_login_saves_and_notifies_the_broker(self):
        from services.auth import upstox_auto_login as m
        pushed = []
        with patch.object(m.UpstoxConfig, "auto_login_configured", return_value=True), patch.object(m.UpstoxConfig, "ACCESS_TOKEN", "old"), \
                patch.object(m, "_token_is_valid", return_value=False), patch.object(m, "_auto_login_get_code", return_value="code") as login, \
                patch.object(m, "UpstoxAuthClient") as client, patch.object(m.UpstoxConfig, "save_access_token") as save:
            client.return_value.exchange_code_for_token.return_value = "new"
            self.assertEqual(m.ensure_fresh_upstox_token(on_token_refreshed=pushed.append), "new")
            login.assert_called_once()
            save.assert_called_once_with("new")
            self.assertEqual(pushed, ["new"])

    def test_a_failed_login_returns_none_instead_of_raising(self):
        from services.auth import upstox_auto_login as m
        with patch.object(m.UpstoxConfig, "auto_login_configured", return_value=True), patch.object(m.UpstoxConfig, "ACCESS_TOKEN", "old"), \
                patch.object(m, "_token_is_valid", return_value=False), patch.object(m, "_auto_login_get_code", side_effect=RuntimeError("selenium died")):
            self.assertIsNone(m.ensure_fresh_upstox_token())

    def test_without_auto_login_configured_nothing_happens(self):
        from services.auth import upstox_auto_login as m
        with patch.object(m.UpstoxConfig, "auto_login_configured", return_value=False), patch.object(m, "_token_is_valid") as probe:
            self.assertIsNone(m.ensure_fresh_upstox_token())
            probe.assert_not_called()


class TestOperatorCommands(unittest.TestCase):
    """Telegram /stop /start /status and per-segment pause/resume, exercised without a live loop."""

    def _runner(self):
        r = SimpleNamespace(positions={"CRUDEOILM": {"direction": "long", "entry_price": 8900.0, "current_stop": 8850.0}},
                            segment_enabled={"commodity": True, "currency": True, "equity": True}, capital=100000.0, trading_enabled=True,
                            use_commodity_regime_filter=True, commodity_regime_ok={"CRUDEOILM": True, "GOLDM": False},
                            use_equity_regime_filter=True, equity_regime_ok=None, calls=[])
        r.stop_trading = lambda now: (r.calls.append("stop"), 1)[1]
        r.start_trading = lambda: r.calls.append("start")
        return r

    def _cmd(self, runner, text):
        from engine import live_dryrun
        sent = []
        with patch.object(live_dryrun.telegram, "send", sent.append):
            action = live_dryrun.handle_operator_command(runner, text, datetime(2026, 9, 24, 12, 0, tzinfo=IST), 7)
        return action, sent

    def test_stop_and_start_and_their_aliases(self):
        r = self._runner()
        for text in ("/stop", "stop", "stop trading"):
            self.assertEqual(self._cmd(r, text)[0], "stop")
        for text in ("/start", "start", "start trading"):
            self.assertEqual(self._cmd(r, text)[0], "start")
        self.assertEqual(r.calls, ["stop"] * 3 + ["start"] * 3)

    def test_stop_says_how_many_positions_it_closed(self):
        _, sent = self._cmd(self._runner(), "/stop")
        self.assertIn("1 open position(s) force-closed", sent[0])

    def test_each_segment_can_be_paused_and_resumed_independently(self):
        r = self._runner()
        for seg in ("commodity", "currency", "equity"):
            self.assertEqual(self._cmd(r, f"/stop {seg}")[0], f"pause:{seg}")
            self.assertFalse(r.segment_enabled[seg])
            self.assertEqual([s for s, v in r.segment_enabled.items() if v and s != seg], [s for s in r.segment_enabled if s != seg])
            self.assertEqual(self._cmd(r, f"/enable {seg}")[0], f"resume:{seg}")
            self.assertTrue(r.segment_enabled[seg])

    def test_status_reports_balance_positions_segments_and_regimes(self):
        r = self._runner()
        r.segment_enabled["currency"] = False
        action, sent = self._cmd(r, "/status")
        self.assertEqual(action, "status")
        for expected in ("₹100,000.00", "CRUDEOILM LONG @ ₹8900.00", "stop ₹8850.00", "Currency: 🛑 PAUSED", "GOLDM: 🔴", "Scan #7", "ENABLED"):
            self.assertIn(expected, sent[0])

    def test_unknown_text_is_ignored(self):
        r = self._runner()
        self.assertEqual(self._cmd(r, "hello there"), (None, []))
        self.assertEqual(r.calls, [])


class TestLiveOrderQuantityMatchesUpstoxLotSize(unittest.TestCase):
    """The cost model's multiplier is not always Upstox's lot size; an order that is not a multiple of Upstox's lot must never be sent."""

    def _trader_and_signal(self, lot_size_multiplier, qty=2):
        import tempfile
        from unittest.mock import MagicMock
        from engine import live_trading
        from engine.database import TradingDB
        from core.strategy import Signal
        broker = MagicMock()
        broker.place_order = MagicMock(return_value="OID")
        with patch.object(live_trading, "_build_symbol_map", lambda: {}):
            trader = live_trading.LiveTrader(broker=broker, db=TradingDB(Path(tempfile.mkdtemp()) / "t.db"), symbols=["GOLDM"], capital=100000.0,
                                             risk_pct=4.0, leverage=5.0)
        sig = MagicMock(spec=Signal)
        sig.symbol, sig.qty, sig.lot_size, sig.direction, sig.instrument_key = "GOLDM", qty, lot_size_multiplier, "long", "MCX_FO|569003"
        return trader, sig, broker

    def test_a_quantity_that_is_not_a_multiple_of_the_upstox_lot_size_is_refused_before_any_order(self):
        from engine import live_trading, margin_rates
        trader, sig, broker = self._trader_and_signal(10, qty=2)                    # GOLDM: 2 lots * multiplier 10 = 20, Upstox lot is 100
        with patch.object(margin_rates, "master_lot_size", return_value=100), patch.object(live_trading.telegram, "send") as tg:
            self.assertFalse(trader._enter(MagicMockStrat(), sig, 5.0, datetime(2026, 9, 24, 12, 0, tzinfo=IST)))
        broker.place_order.assert_not_called()
        self.assertIn("REFUSED", tg.call_args[0][0])

    def test_a_correct_multiple_is_not_blocked_by_the_guard(self):
        from engine import live_trading, margin_rates
        trader, sig, broker = self._trader_and_signal(10, qty=1)                    # CRUDEOILM-like: 1 lot * 10 = 10, Upstox lot 10
        with patch.object(margin_rates, "master_lot_size", return_value=10), patch.object(live_trading.telegram, "send") as tg:
            try:
                trader._enter(MagicMockStrat(), sig, 5.0, datetime(2026, 9, 24, 12, 0, tzinfo=IST))
            except Exception:
                pass                                                                # later steps use mocks; only the guard is under test
        for call in tg.call_args_list:
            self.assertNotIn("REFUSED", call[0][0])


class MagicMockStrat:
    product, id_prefix, name, uses_leverage = "I", "X", "scalping", True


class TestStaleFeedGuard(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 12, 0, tzinfo=IST)

    def _runner(self):
        from types import SimpleNamespace
        from engine.live_dryrun import DryRunner
        return SimpleNamespace(_stale_logged={}, fresh=DryRunner._candles_fresh)

    def _fresh(self, minutes_old, timeframe=("minutes", 5)):
        r = self._runner()
        strat = SimpleNamespace(timeframe=timeframe)
        ts = (self.NOW - timedelta(minutes=minutes_old)).isoformat()
        return r.fresh(r, "CRUDEOILM", [{"timestamp": ts}], self.NOW, strat)

    def test_a_current_or_forming_bar_is_fresh(self):
        self.assertTrue(self._fresh(0))
        self.assertTrue(self._fresh(4))
        self.assertTrue(self._fresh(12))                   # limit is two bars + 3 minutes

    def test_a_feed_that_stopped_updating_blocks_entries(self):
        self.assertFalse(self._fresh(14))
        self.assertFalse(self._fresh(45))

    def test_daily_bar_strategies_and_empty_lists_are_exempt(self):
        self.assertTrue(self._fresh(60 * 24 * 3, timeframe=("days", 1)))
        r = self._runner()
        self.assertTrue(r.fresh(r, "X", [], self.NOW, SimpleNamespace(timeframe=("minutes", 5))))
