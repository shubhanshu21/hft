"""The nightly trade audit must catch phantom exits (2026-09-23) and must never invent one."""
import unittest

import pandas as pd

from engine.trade_audit import audit_trade, format_report

TZ = "+05:30"


def _candles(rows, step=1):
    """rows: [(HH:MM, open, high, low, close)] on 2026-09-23."""
    df = pd.DataFrame([{"timestamp": f"2026-09-23T{t}:00{TZ}", "open": o, "high": h, "low": l, "close": c, "volume": 100} for t, o, h, l, c in rows])
    df["ts"] = pd.to_datetime(df["timestamp"])
    return df


# Real 1-minute CRUDEOILM bars around trade #16 (long 8864 at 20:59:00, "stopped" at 8814.29 at 20:59:35)
CRUDE = _candles([("20:55", 8815, 8817, 8811, 8812), ("20:56", 8812, 8835, 8813, 8832), ("20:57", 8833, 8845, 8833, 8842),
                  ("20:58", 8850, 8870, 8850, 8864), ("20:59", 8860, 8894, 8860, 8891), ("21:00", 8889, 8894, 8873, 8877),
                  ("21:01", 8874, 8880, 8868, 8878), ("21:02", 8874, 8885, 8872, 8884)])


def _trade(**over):
    t = {"trade_id": 16, "position_id": "P1", "symbol": "CRUDEOILM", "direction": "long", "entry_price": 8864.0, "exit_price": 8814.29,
         "entry_dt": f"2026-09-23T20:59:00.4{TZ}", "exit_dt": f"2026-09-23T20:59:35.0{TZ}", "exit_reason": "initial_stop",
         "breakeven_price": 8894.0, "target_price": 8950.0}
    t.update(over)
    return t


class TestPhantomExits(unittest.TestCase):
    def kinds(self, res):
        return [f.kind for f in res.findings]

    def test_the_real_crude_phantom_stop_is_flagged(self):
        self.assertEqual(self.kinds(audit_trade(_trade(), CRUDE)), ["phantom_exit"])

    def test_a_stop_that_really_traded_passes(self):
        legit = _trade(trade_id=18, exit_price=8871.0, entry_price=8907.0, entry_dt=f"2026-09-23T20:56:10{TZ}", exit_dt=f"2026-09-23T21:01:30{TZ}")
        self.assertNotIn("phantom_exit", self.kinds(audit_trade(legit, CRUDE)))

    def test_short_stops_are_symmetric(self):
        short = _trade(direction="short", entry_price=8850.0, exit_price=8990.0)       # stop far above anything that traded
        self.assertEqual(self.kinds(audit_trade(short, CRUDE)), ["phantom_exit"])

    def test_a_pre_entry_low_in_an_earlier_bar_can_never_justify_a_stop(self):
        # 8811 traded at 20:55, four minutes before entry: a stop at 8812 must still be flagged
        self.assertEqual(self.kinds(audit_trade(_trade(exit_price=8812.0), CRUDE)), ["phantom_exit"])

    def test_a_take_profit_never_reached_is_flagged_and_a_reached_one_passes(self):
        self.assertEqual(self.kinds(audit_trade(_trade(exit_reason="take_profit", exit_price=8950.0), CRUDE)), ["phantom_target"])
        self.assertEqual(self.kinds(audit_trade(_trade(exit_reason="take_profit", exit_price=8890.0), CRUDE)), [])

    def test_a_breakeven_exit_needs_the_breakeven_level_to_have_been_reached(self):
        self.assertIn("unarmed_breakeven", self.kinds(audit_trade(_trade(exit_reason="be_stop", exit_price=8870.0, breakeven_price=9500.0), CRUDE)))
        self.assertNotIn("unarmed_breakeven", self.kinds(audit_trade(_trade(exit_reason="be_stop", exit_price=8870.0, breakeven_price=8890.0,
                                                                            exit_dt=f"2026-09-23T21:01:30{TZ}"), CRUDE)))

    def test_timeout_exit_must_lie_inside_the_exit_bar(self):
        ok = _trade(exit_reason="timeout_exit", exit_price=8878.0, exit_dt=f"2026-09-23T21:01:20{TZ}")
        self.assertEqual(self.kinds(audit_trade(ok, CRUDE)), [])
        bad = _trade(exit_reason="timeout_exit", exit_price=9100.0, exit_dt=f"2026-09-23T21:01:20{TZ}")
        self.assertEqual(self.kinds(audit_trade(bad, CRUDE)), ["exit_outside_bar"])

    def test_an_entry_price_far_from_anything_that_traded_is_flagged(self):
        self.assertIn("entry_off_market", self.kinds(audit_trade(_trade(entry_price=9300.0, exit_reason="timeout_exit", exit_price=8878.0,
                                                                        exit_dt=f"2026-09-23T21:01:20{TZ}"), CRUDE)))

    def test_missing_candles_are_reported_as_no_data_never_as_a_pass(self):
        res = audit_trade(_trade(), None)
        self.assertEqual(self.kinds(res), ["no_data"])
        self.assertFalse(res.findings[0].flagged)

    def test_fill_offset_is_reported_in_basis_points(self):
        res = audit_trade(_trade(entry_price=8864.0, exit_reason="timeout_exit", exit_price=8878.0, exit_dt=f"2026-09-23T21:01:20{TZ}"), CRUDE)
        self.assertAlmostEqual(res.fill_offset_bps, (8864.0 / 8891.0 - 1) * 1e4, places=6)     # vs the entry bar's (20:59) close, long


class TestReport(unittest.TestCase):
    def test_summary_counts_flags_and_no_data(self):
        text, n = format_report([audit_trade(_trade(), CRUDE), audit_trade(_trade(trade_id=2), None)])
        self.assertEqual(n, 1)
        self.assertIn("2 trade(s), 1 flagged, 1 without candle data", text)
        self.assertIn("FLAG #16 CRUDEOILM: phantom_exit", text)


if __name__ == "__main__":
    unittest.main()
