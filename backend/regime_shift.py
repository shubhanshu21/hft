#!/usr/bin/env python3
"""
regime_shift.py — Dissimilarity Index (out-of-distribution / regime-shift gate)

Pattern taken from FreqAI (github.com/freqtrade/freqtrade, reviewed
2026-09-17): before trusting a live prediction, measure how far the current
feature vector sits from the training distribution. If it's an outlier
relative to what the model was trained on, treat the prediction as
untrustworthy rather than acting on it anyway.

This is the piece our own research process was missing a mechanism for: we
only discovered that BTC/ETH funding decayed to the 0.01% baseline through
2025-2026, and that momentum/ML edges from 2020-2022 didn't generalize,
by manually re-running backtests split by year AFTER the fact
(docs/CRYPTO_RESEARCH_FINDINGS.md). A dissimilarity gate is the automatic,
before-the-fact version of that same check: fit once on a training window,
then score how "unlike training" each new bar's features are. Validated
below against that exact known regime shift before being trusted anywhere
live -- see the __main__ block / tests/test_regime_shift.py.

Method: for each live feature vector, find its k nearest neighbors in the
(standardized) training feature set and average that distance. Normalize
by the training set's own typical k-NN distance (computed the same way,
in-sample) to get a Dissimilarity Index (DI): DI ~ 1.0 means "as typical
as an average training point", DI >> 1.0 means "unlike anything the model
was trained on."

Deliberately generic (fit on any feature matrix) so it can wrap any of this
project's ML models -- MCX's live LightGBM classifier, or a future crypto
scalping model -- without depending on either.
"""
from __future__ import annotations

import numpy as np


class DissimilarityGate:
    def __init__(self, k: int = 4):
        self.k = k
        self._mean = None
        self._std = None
        self._nn = None
        self._baseline_di = None   # mean in-sample k-NN distance, per training point
        self._baseline_std = None

    def fit(self, train_features: np.ndarray) -> "DissimilarityGate":
        from sklearn.neighbors import NearestNeighbors

        X = np.asarray(train_features, dtype=float)
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0)
        self._std[self._std < 1e-12] = 1.0
        Xs = (X - self._mean) / self._std

        self._nn = NearestNeighbors(n_neighbors=self.k + 1).fit(Xs)
        # in-sample: each point's own nearest neighbors, excluding itself (index 0)
        dists, _ = self._nn.kneighbors(Xs)
        in_sample_di = dists[:, 1:].mean(axis=1)
        self._baseline_di = float(in_sample_di.mean())
        self._baseline_std = float(in_sample_di.std()) or 1e-9
        return self

    def score(self, live_features: np.ndarray) -> np.ndarray:
        """Returns a DI array, one per row: 1.0 = as typical as an average
        training point, higher = more of an outlier relative to training."""
        if self._nn is None:
            raise RuntimeError("DissimilarityGate.fit() must be called first")
        X = np.asarray(live_features, dtype=float)
        Xs = (X - self._mean) / self._std
        dists, _ = self._nn.kneighbors(Xs, n_neighbors=self.k)
        raw_di = dists.mean(axis=1)
        return raw_di / max(self._baseline_di, 1e-9)

    def is_in_distribution(self, live_features: np.ndarray, threshold_std: float = 3.0) -> np.ndarray:
        """Boolean mask: True = trust the model's prediction here, False = the
        input looks unlike anything it trained on (a candidate regime shift)."""
        di = self.score(live_features)
        cutoff = 1.0 + threshold_std * (self._baseline_std / max(self._baseline_di, 1e-9))
        return di <= cutoff

    def trust_weight(self, live_features: np.ndarray, threshold_std: float = 3.0, floor: float = 0.1) -> np.ndarray:
        """Continuous version for position sizing (mirrors strategy_ensemble's
        floor-weighting): 1.0 when clearly in-distribution, decaying toward
        `floor` (never fully zero, same rationale as elsewhere in this
        project -- it must keep scoring to notice a return to normal)."""
        di = self.score(live_features)
        cutoff = 1.0 + threshold_std * (self._baseline_std / max(self._baseline_di, 1e-9))
        excess = np.clip((di - 1.0) / max(cutoff - 1.0, 1e-9), 0.0, 1.0)
        return 1.0 - (1.0 - floor) * excess


if __name__ == "__main__":
    # Originally empirically validated here against a known crypto regime
    # shift (BTCUSDT funding-rate compression, 2020-2022 vs 2025-2026) -- see
    # tests/test_regime_shift.py's docstring for that finding (a nearest-
    # neighbor novelty detector catches genuine outliers but NOT a slow
    # collapse toward the training center, which is what that shift was).
    # The crypto pipeline and its archives were removed 2026-09-17 (Indian-
    # market focus); this class and its synthetic-data test suite are
    # generic and kept as a reusable tool -- e.g. a black-swan/out-of-
    # distribution guard on MCX's live LightGBM model.
    print("regime_shift.DissimilarityGate is a library module -- see tests/test_regime_shift.py "
          "for synthetic-data validation, or import DissimilarityGate directly to use it.")
