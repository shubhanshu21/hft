import unittest
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime_shift import DissimilarityGate


class TestDissimilarityGate(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)
        self.train = self.rng.normal(0, 1, size=(2000, 3))
        self.gate = DissimilarityGate(k=5).fit(self.train)

    def test_in_distribution_scores_near_one(self):
        live = self.rng.normal(0, 1, size=(500, 3))
        di = self.gate.score(live)
        self.assertLess(abs(di.mean() - 1.0), 0.2)
        self.assertLess((~self.gate.is_in_distribution(live)).mean(), 0.05)

    def test_far_outlier_is_flagged(self):
        """This is the case the gate is actually designed for: a feature
        vector unlike anything seen in training (e.g. a black-swan move)."""
        live = self.rng.normal(20, 1, size=(500, 3))
        di = self.gate.score(live)
        self.assertGreater(di.mean(), 10.0)
        self.assertGreater((~self.gate.is_in_distribution(live)).mean(), 0.95)

    def test_variance_compression_is_NOT_flagged(self):
        """Documented limitation, found empirically against real BTC funding
        data (see regime_shift.py's __main__ block): a distribution that
        collapses toward the training center (as BTC/ETH funding did toward
        the 0.01% baseline through 2025-2026) looks MORE typical by k-NN
        distance, not less. This is a nearest-neighbor novelty detector, not
        a decay detector -- it catches genuine anomalies, not a slow fade
        toward homogeneity. Asserted explicitly so a future change to the
        method doesn't silently start over- or under-claiming what it does."""
        live = self.rng.normal(0, 0.1, size=(500, 3))
        di = self.gate.score(live)
        self.assertLess(di.mean(), 1.0)
        self.assertEqual((~self.gate.is_in_distribution(live)).sum(), 0)

    def test_trust_weight_bounds(self):
        live = np.vstack([
            self.rng.normal(0, 1, size=(100, 3)),
            self.rng.normal(20, 1, size=(100, 3)),
        ])
        w = self.gate.trust_weight(live, floor=0.1)
        self.assertTrue(np.all(w >= 0.1 - 1e-9))
        self.assertTrue(np.all(w <= 1.0 + 1e-9))
        # the far-outlier half should be weighted at (or near) the floor
        self.assertLess(w[100:].mean(), w[:100].mean())


if __name__ == "__main__":
    unittest.main()
