"""engine/backtest_api.today_plan -- the account page's "Today" box: what each equity engine decided this morning and why (2026-11-18).
State files live in a temp folder."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from engine import backtest_api as A

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=A.IST)


class TestTodayPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, **st):
        (self.dir / name).write_text(json.dumps(st))

    def plan(self, account):
        return " ".join(A.today_plan(account, NOW, state_dir=self.dir))

    def test_breakout_weak_market_says_why_and_what_it_would_buy(self):
        self.write("equity_breakout_state.json", day="2026-11-18", breadth=29.3, healthy=False, fresh=True, d55=["BEARDSELL"],
                   nr7=[["MINDSPACE", 502.5]], dead=[])
        t = self.plan("EQUITY_BREAKOUT")
        self.assertIn("No new buys today: market health 29%", t)
        self.assertIn("BEARDSELL (55-day high), MINDSPACE if it trades above Rs502.50", t)

    def test_breakout_healthy_and_stale(self):
        self.write("equity_breakout_state.json", day="2026-11-18", breadth=61.0, healthy=True, fresh=True, d55=["A"], nr7=[], dead=["OLD"])
        t = self.plan("EQUITY_BREAKOUT")
        self.assertIn("Buying today", t); self.assertIn("A (55-day high)", t); self.assertIn("20 trading days): OLD", t)
        self.write("equity_breakout_state.json", day="2026-11-18", breadth=61.0, healthy=True, fresh=False, last_day="2026-11-16", d55=[], nr7=[])
        self.assertIn("price data ends 2026-11-16", self.plan("EQUITY_BREAKOUT"))

    def test_nothing_from_today_shows_nothing(self):
        self.write("equity_breakout_state.json", day="2026-11-17", breadth=29.3, healthy=False)
        self.assertEqual(A.today_plan("EQUITY_BREAKOUT", NOW, state_dir=self.dir), [])
        self.assertEqual(A.today_plan("CRYPTO_DIP_REVERSAL", NOW, state_dir=self.dir), [])     # no file at all

    def test_results_gap_and_earnings_drift(self):
        self.write("results_gap_state.json", entry_date="2026-11-18", last_check={"candidates": 3, "market_gap": -0.001, "signals": []})
        self.write("earnings_drift_state.json", day="2026-11-18", sell={}, buy=[], breadth=29.3, fresh=True)
        t = self.plan("RESULTS_GAP")
        self.assertIn("3 liquid stock(s) had results; none opened 7%+", t)
        self.assertIn("Earnings drift: no new buys, market health 29%", t)
        self.write("results_gap_state.json", entry_date="2026-11-18", last_check={"candidates": 3, "market_gap": 0.002, "signals": ["XYZ"]})
        self.write("earnings_drift_state.json", day="2026-11-18", sell={}, buy=["GOOD"], breadth=70.0, fresh=True)
        t = self.plan("RESULTS_GAP")
        self.assertIn("bought XYZ", t); self.assertIn("buying at the open GOOD", t)

    def test_no_prices_at_0930(self):
        self.write("results_gap_state.json", entry_date="2026-11-18", last_check={"candidates": 3, "market_gap": None, "signals": []})
        self.assertIn("no usable prices", self.plan("RESULTS_GAP"))


if __name__ == "__main__":
    unittest.main()
