"""
tests/test_sector_correlation.py — Unit tests for Sector Correlation & Concentration Gate.
"""
import unittest
from strategy.sector_correlation import SectorCorrelationGate, NIFTY_SECTOR_MAP


class TestSectorCorrelation(unittest.TestCase):

    def setUp(self):
        self.gate = SectorCorrelationGate(max_per_sector=1)

    def test_nifty_symbol_mappings(self):
        self.assertEqual(SectorCorrelationGate.get_sector("TCS"), "IT")
        self.assertEqual(SectorCorrelationGate.get_sector("INFY"), "IT")
        self.assertEqual(SectorCorrelationGate.get_sector("HDFCBANK"), "BANKING_FINANCE")
        self.assertEqual(SectorCorrelationGate.get_sector("RELIANCE"), "ENERGY")
        self.assertEqual(SectorCorrelationGate.get_sector("CRUDEOILM"), "COMMODITY")
        self.assertEqual(SectorCorrelationGate.get_sector("USDINR"), "CURRENCY")

    def test_sector_gate_allows_first_entry(self):
        active = []
        allowed, reason = self.gate.can_enter("TCS", active)
        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")

    def test_sector_gate_blocks_second_entry_in_same_sector(self):
        active = ["TCS"]
        # INFY is also IT -> should be blocked under max_per_sector=1
        allowed, reason = self.gate.can_enter("INFY", active)
        self.assertFalse(allowed)
        self.assertIn("Sector cap reached", reason)

    def test_sector_gate_allows_different_sectors(self):
        active = ["TCS"]  # IT
        # HDFCBANK is BANKING_FINANCE -> should be allowed
        allowed, reason = self.gate.can_enter("HDFCBANK", active)
        self.assertTrue(allowed)

    def test_sector_gate_with_dict_positions(self):
        active = [
            {"symbol": "RELIANCE", "qty": 10},
            {"symbol": "TCS", "qty": 5},
        ]
        # Another energy stock should be blocked
        allowed, reason = self.gate.can_enter("ONGC", active)
        self.assertFalse(allowed)

        # A pharma stock should be allowed
        allowed, reason = self.gate.can_enter("SUNPHARMA", active)
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
