import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.utils.position_reconciliation import find_discrepancies


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

    def test_position_own_instrument_key_wins_over_stale_symbol_map(self):
        # SYMBOL_MAP says CRUDEOILM -> MCX_FO|1 (e.g. after a contract
        # rollover), but this position was actually opened on the OLD
        # contract MCX_FO|999 -- must be checked against that, not whatever
        # CRUDEOILM currently resolves to.
        pos = _tracked("long", 5)
        pos["instrument_key"] = "MCX_FO|999"
        tracked = {"CRUDEOILM": pos}
        broker = _FakeBroker({"MCX_FO|999": 5, "MCX_FO|1": 0})
        self.assertEqual(find_discrepancies(broker, tracked, SYMBOL_MAP), {})

    def test_position_own_instrument_key_detects_real_discrepancy_stale_symbol_map_would_miss(self):
        pos = _tracked("long", 5)
        pos["instrument_key"] = "MCX_FO|999"
        tracked = {"CRUDEOILM": pos}
        # If this incorrectly checked SYMBOL_MAP's MCX_FO|1 instead, it would
        # see 0 there too and wrongly conclude "matches" -- the whole point
        # of pinning instrument_key is to catch this against the real
        # contract that was actually traded.
        broker = _FakeBroker({"MCX_FO|999": 0, "MCX_FO|1": 0})
        disc = find_discrepancies(broker, tracked, SYMBOL_MAP)
        self.assertEqual(disc["CRUDEOILM"]["kind"], "externally_closed")


if __name__ == "__main__":
    unittest.main()
