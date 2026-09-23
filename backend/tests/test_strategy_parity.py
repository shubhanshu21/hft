"""Behaviour lock for the strategy refactor.

The golden files were recorded from the code as it was BEFORE decisions moved into Strategy objects, and the
strategy-driven code reproduces them exactly:

  * live_scenarios.json -- what would reach the broker (side, quantity, product, tag), the DB rows and the alert
    kinds for scripted real-order entries, slippage re-anchoring, rejections, ambiguous fills and exits.
  * replay_quick.json   -- 152 orders / 76 trades from replaying DryRunner.scan() bar by bar over recorded
    commodity, currency and equity data (every exit type exercised).

If you change trading behaviour ON PURPOSE (a threshold, an exit rule), regenerate the golden that moved:

    python3 -m tests.replay_harness quick tests/golden/replay_quick.json
    python3 -m tests.live_scenarios tests/golden/live_scenarios.json

The replay takes about a minute, so it only runs with RUN_REPLAY=1.
"""
import json
import os
import re
import unittest
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden"


def _kind(messages):
    out = []
    for m in messages:
        found = re.search(r"<b>(.*?)</b>", m)
        out.append(found.group(1).split(" — ")[0].replace("(LIVE)", "").strip().split(" [")[0] if found else m[:20])
    return out


class TestRealOrderBehaviourIsUnchanged(unittest.TestCase):
    def test_every_scripted_live_scenario_matches_the_recorded_behaviour(self):
        from tests.live_scenarios import run
        golden = json.loads((GOLDEN / "live_scenarios.json").read_text())
        now = run()
        for section in ("entries", "exits"):
            self.assertEqual(sorted(now[section]), sorted(golden[section]))
            for name, expected in golden[section].items():
                got = dict(now[section][name])
                want = dict(expected)
                # alert wording is generic now; the KIND of alert (filled / insufficient funds / unknown status...) must not change
                self.assertEqual(_kind(got.pop("alerts")), _kind(want.pop("alerts")), f"{section}/{name}: alert kinds")
                for key in want:
                    self.assertEqual(got[key], want[key], f"{section}/{name}: {key}")


@unittest.skipUnless(os.environ.get("RUN_REPLAY"), "slow (~1 min): set RUN_REPLAY=1")
class TestPaperRunnerBehaviourIsUnchanged(unittest.TestCase):
    def test_replay_reproduces_every_order_and_trade(self):
        from tests import replay_harness as h
        golden = json.loads((GOLDEN / "replay_quick.json").read_text())
        now = json.loads(json.dumps(h.run_replay(days=h.QUICK_DAYS, instruments=h.QUICK_INSTRUMENTS), default=str))
        self.assertEqual(now["orders"], golden["orders"])
        self.assertEqual(now["trades"], golden["trades"])

        def entry_without_strategy_tag(alert):
            return {"entry": {k: v for k, v in alert["entry"].items() if k != "strategy"}} if "entry" in alert else alert
        self.assertEqual([entry_without_strategy_tag(a) for a in now["alerts"]], [entry_without_strategy_tag(a) for a in golden["alerts"]])


if __name__ == "__main__":
    unittest.main()
