import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.position_reconciliation import find_discrepancies


class _FakeBroker:
    def __init__(self, positions: dict[str, int] | None):
        self._positions = positions

    def get_broker_positions(self):
        return self._positions


SYMBOL_MAP = {"CRUDEOILM": "MCX_FO|1", "GOLDM": "MCX_FO|2", "USDINR": "NCD_FO|3"}


def _tracked(direction="long", qty=5):
    return {"direction": direction, "qty": qty}


class TestFindDiscrepancies(unittest.TestCase):
    def test_no_discrepancy_when_matching(self):
        tracked = {"CRUDEOILM": _tracked("long", 5)}
        broker = _FakeBroker({"MCX_FO|1": 5})
        self.assertEqual(find_discrepancies(broker, tracked, SYMBOL_MAP), {})

    def test_externally_closed_when_broker_flat(self):
        tracked = {"CRUDEOILM": _tracked("long", 5)}
        broker = _FakeBroker({})  # Upstox omits flat instruments entirely
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "externally_closed")
        self.assertEqual(disc["CRUDEOILM"]["broker_qty"], 0)

    def test_externally_reduced_on_partial_close(self):
        tracked = {"CRUDEOILM": _tracked("long", 5)}
        broker = _FakeBroker({"MCX_FO|1": 2})  # same direction, smaller size
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "externally_reduced")

    def test_externally_reversed_on_opposite_direction(self):
        tracked = {"CRUDEOILM": _tracked("long", 5)}
        broker = _FakeBroker({"MCX_FO|1": -5})  # broker shows short
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "externally_reversed")

    def test_externally_reversed_on_larger_same_direction(self):
        tracked = {"CRUDEOILM": _tracked("long", 5)}
        broker = _FakeBroker({"MCX_FO|1": 8})  # same direction but bigger -- extra real orders happened
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "externally_reversed")

    def test_short_position_matches_correctly(self):
        tracked = {"GOLDM": _tracked("short", 3)}
        broker = _FakeBroker({"MCX_FO|2": -3})
        self.assertEqual(find_discrepancies(broker, tracked, SYMBOL_MAP), {})

    def test_unknown_when_broker_api_fails(self):
        tracked = {"CRUDEOILM": _tracked("long", 5), "GOLDM": _tracked("short", 3)}
        broker = _FakeBroker(None)  # simulates get_broker_positions() failure
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "unknown")
        self.assertEqual(disc["GOLDM"]["kind"], "unknown")

    def test_unmapped_symbol_is_skipped_not_flagged(self):
        tracked = {"UNMAPPED": _tracked("long", 5)}
        broker = _FakeBroker({})
        self.assertEqual(find_discrepancies(broker, tracked, SYMBOL_MAP), {})


if __name__ == "__main__":
    unittest.main()
