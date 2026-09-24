import unittest
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from markets.commodity import costs as commodity_costs
from markets.currency import costs as currency_costs

# How long a statutory-rate module can go unreviewed before this test starts
# failing on purpose. Indian statutory rates (STT/CTT/stamp duty/exchange fees)
# change via budget announcements and exchange circulars with no push
# notification to this codebase -- found the hard way 2026-09-18, when the
# 2026-04-01 STT hike on index futures (0.02%->0.05%) was only caught by a
# manual web search while building an unrelated feature, five and a half
# months after it took effect. Nothing else in this project would ever have
# noticed. This doesn't verify rates automatically (no reliable machine-
# readable source exists) -- it forces a periodic HUMAN re-check by failing
# loudly once one is overdue, rather than staying silent indefinitely.
MAX_STALENESS_DAYS = 180


class TestRateFreshness(unittest.TestCase):
    def _assert_fresh(self, module):
        verified = date.fromisoformat(module.RATES_LAST_VERIFIED)
        age_days = (date.today() - verified).days
        self.assertLessEqual(
            age_days, MAX_STALENESS_DAYS,
            f"{module.__name__}.RATES_LAST_VERIFIED ({module.RATES_LAST_VERIFIED}) is "
            f"{age_days} days old (limit {MAX_STALENESS_DAYS}). Re-verify every rate in "
            f"this module against a current public source (exchange circular / budget "
            f"notification), then bump RATES_LAST_VERIFIED -- do not bump it without "
            f"actually re-checking."
        )

    def test_commodity_costs_fresh(self):
        self._assert_fresh(commodity_costs)

    def test_currency_costs_fresh(self):
        self._assert_fresh(currency_costs)


if __name__ == "__main__":
    unittest.main()
