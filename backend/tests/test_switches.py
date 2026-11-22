"""core/switches.py -- one on/off switch per equity strategy in backend/.env (2026-11-19). Off stops NEW buys only."""
import unittest
from unittest import mock

from core import switches


class TestSwitches(unittest.TestCase):
    def test_on_unless_clearly_off(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertTrue(switches.on("RESULTS_GAP"))                       # not set: on
        for v in ("off", "OFF", " 0 ", "false", "no"):
            with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": v}):
                self.assertFalse(switches.on("MOMENTUM"), v)
        for v in ("on", "1", "yes", "true"):
            with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": v}):
                self.assertTrue(switches.on("MOMENTUM"), v)

    def test_status_lists_every_equity_strategy(self):
        self.assertEqual(set(switches.status()), {"RESULTS_GAP", "EARNINGS_DRIFT", "BREAKOUT", "MOMENTUM", "RG_MOMENTUM"})

class TestClose(unittest.TestCase):
    def test_close_is_off_and_closing(self):
        from core import switches
        with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": "close"}):
            self.assertFalse(switches.on("MOMENTUM"))
            self.assertTrue(switches.closing("MOMENTUM"))
        with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": "off"}):
            self.assertFalse(switches.closing("MOMENTUM"))
