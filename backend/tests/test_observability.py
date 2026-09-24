"""Scorecard maths and the status page."""
import unittest
from datetime import datetime, timedelta, timezone

from engine import scorecard, status

IST = timezone(timedelta(hours=5, minutes=30))


def _t(sym, net):
    return {"symbol": sym, "net_pnl": net}


class TestScorecard(unittest.TestCase):
    def test_todays_shape_needs_a_high_win_rate(self):
        # seven small breakeven wins of ~Rs845 and three full stops of ~Rs1,547: the payoff that made a 8/12 day only +Rs1.2k
        s = scorecard.summarize([_t("X", 845)] * 7 + [_t("X", -1547)] * 3)
        self.assertAlmostEqual(s["win_pct"], 70.0)
        self.assertAlmostEqual(s["need_pct"], 100 * 1547 / (845 + 1547), places=6)        # ~64.7% needed to break even
        self.assertAlmostEqual(s["net"], 7 * 845 - 3 * 1547)
        self.assertAlmostEqual(s["pf"], 7 * 845 / (3 * 1547))
        self.assertAlmostEqual(s["payoff"], 845 / 1547)

    def test_edge_cases_do_not_divide_by_zero(self):
        self.assertEqual(scorecard.summarize([])["n"], 0)
        only_wins = scorecard.summarize([_t("X", 100), _t("X", 50)])
        self.assertEqual((only_wins["need_pct"], only_wins["pf"], only_wins["payoff"]), (0.0, float("inf"), float("inf")))
        only_losses = scorecard.summarize([_t("X", -100)])
        self.assertEqual((only_losses["win_pct"], only_losses["pf"]), (0.0, 0.0))

    def test_breakeven_trade_counts_as_not_a_win(self):
        self.assertEqual(scorecard.summarize([_t("X", 0.0)])["wins"], 0)

    def test_grouping_by_symbol_and_the_telegram_text(self):
        hist = [_t("CRUDEOILM", 700), _t("CRUDEOILM", -1500), _t("USDINR", 300)]
        self.assertEqual(list(scorecard.by_symbol(hist)), ["CRUDEOILM", "USDINR"])
        text = scorecard.format_scorecard(hist[:2], hist)
        self.assertIn("<pre>", text)
        self.assertIn("CRUDEOILM", text)
        self.assertIn("SINCE START (3 trades)", text)
        self.assertIsNone(scorecard.format_scorecard([], []))                              # nothing to say -> send nothing

    def test_a_day_without_trades_still_shows_history(self):
        text = scorecard.format_scorecard([], [_t("X", 5.0)])
        self.assertIn("(no trades)", text)


class TestStatusRender(unittest.TestCase):
    NOW = datetime(2026, 9, 24, 19, 36, tzinfo=IST)

    def _state(self, **over):
        s = {"now": self.NOW, "service": "active", "heartbeat": {"phase": "sleep", "ts": "2026-09-24T19:36:09+05:30"}, "heartbeat_problem": None,
             "capital": 101247.0, "initial": 100000.0, "day_trades": 12, "day_pnl": 1247.0, "open_positions": [], "control": {},
             "token_hours": 7.9, "last_backup": self.NOW - timedelta(minutes=3)}
        s.update(over)
        return s

    def test_a_healthy_state(self):
        text = status.render(self._state())
        for expected in ("active", "sleep at 19:36:09", "enabled", "Rs101,247.00", "+1,247.00", "12 trade(s)", "none", "expires in 7.9 h", "0.1 h ago"):
            self.assertIn(expected, text)

    def test_problems_are_visible(self):
        text = status.render(self._state(heartbeat_problem="no heartbeat for 900s past its deadline", control={"enabled": False},
                                         token_hours=-1.0, last_backup=None,
                                         open_positions=[{"symbol": "CRUDEOILM", "direction": "long", "qty": 5, "entry_price": 8900.0,
                                                          "current_stop": 8850.0, "target_price": 8990.0}]))
        for expected in ("no heartbeat for 900s", "HALTED", "EXPIRED", "NONE", "CRUDEOILM", "8850"):
            self.assertIn(expected, text)

    def test_no_heartbeat_file_yet(self):
        self.assertIn("no heartbeat file yet", status.render(self._state(heartbeat=None)))


if __name__ == "__main__":
    unittest.main()


class TestPerSymbolRiskOverrides(unittest.TestCase):
    """Coded per-symbol risk (CRUDEOILM 3%, SILVER 5%) used to beat .env silently; an env var per symbol now wins."""

    def _get(self, sym, env=None):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from engine import live_dryrun
        stub = SimpleNamespace(risk_pct=4.0, leverage=5.0)
        with patch.dict(os.environ, {"COMMODITY_RISK_PCT": "10", "COMMODITY_LEVERAGE": "10", **(env or {})}, clear=False):
            for k in ("CRUDEOILM_RISK_PCT", "SILVER_RISK_PCT", "CRUDEOILM_LEVERAGE"):
                if not env or k not in env:
                    os.environ.pop(k, None)
            return live_dryrun.DryRunner._get_segment_risk_and_leverage(stub, sym)

    def test_without_a_symbol_env_var_the_coded_override_still_applies(self):
        self.assertEqual(self._get("CRUDEOILM"), (3.0, 10.0))
        self.assertEqual(self._get("SILVER"), (5.0, 10.0))
        self.assertEqual(self._get("GOLDM"), (10.0, 10.0))                       # no override: the segment value

    def test_a_symbol_env_var_beats_the_coded_override(self):
        self.assertEqual(self._get("CRUDEOILM", {"CRUDEOILM_RISK_PCT": "10"}), (10.0, 10.0))
        self.assertEqual(self._get("CRUDEOILM", {"CRUDEOILM_LEVERAGE": "7"}), (3.0, 7.0))
