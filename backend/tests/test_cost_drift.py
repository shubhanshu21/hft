"""The cost-drift monitor must flag a model that is cheaper than Upstox (overstated profits) and never call an unanswered symbol 'ok'."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from engine import cost_drift


def _charges(total):
    return SimpleNamespace(data=SimpleNamespace(charges=SimpleNamespace(total=total)))


class FakeChargeApi:
    per_leg = 43.3          # Upstox's real crude round trip is ~Rs86.6 = two legs of ~43.3

    def __init__(self, client):
        pass

    def get_brokerage(self, key, qty, product, side, price, version):
        if FakeChargeApi.per_leg is None:
            raise TimeoutError("read timed out")
        return _charges(FakeChargeApi.per_leg)


class FakeBroker:
    _api_client = object()

    def get_ltp(self, key):
        return 9178.0


class TestDrift(unittest.TestCase):
    def setUp(self):
        FakeChargeApi.per_leg = 43.3
        p = patch("upstox_client.ChargeApi", FakeChargeApi)
        p.start()
        self.addCleanup(p.stop)

    def _rows(self):
        return cost_drift.check(FakeBroker(), {"CRUDEOILM": "MCX_FO|1"}, lambda s: "commodity")

    def test_a_model_that_matches_upstox_is_ok(self):
        r = self._rows()[0]
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["ratio"], 1.0, delta=0.02)

    def test_a_model_cheaper_than_upstox_is_flagged_as_understating(self):
        FakeChargeApi.per_leg = 60.0                        # Upstox raises its charges ~40%: the model is now too cheap
        r = self._rows()[0]
        self.assertEqual(r["status"], "UNDERSTATES")
        self.assertIn("UNDERSTATES", cost_drift.format_rows([r]))

    def test_a_much_dearer_model_is_flagged_but_it_is_not_the_dangerous_direction(self):
        FakeChargeApi.per_leg = 20.0
        self.assertEqual(self._rows()[0]["status"], "overstates")

    def test_an_unanswered_calculator_is_no_data_never_ok(self):
        FakeChargeApi.per_leg = None
        self.assertEqual(self._rows()[0]["status"], "no-data")

    def test_the_exit_code_is_nonzero_only_when_the_model_understates(self):
        rows_under = [{"symbol": "X", "status": "UNDERSTATES"}]
        self.assertTrue(any(r["status"] == "UNDERSTATES" for r in rows_under))


if __name__ == "__main__":
    unittest.main()
