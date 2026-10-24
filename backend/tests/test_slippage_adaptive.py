"""
tests/test_slippage_adaptive.py — Unit tests for adaptive slippage estimation module.
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from core.slippage import (
    adaptive_slippage_per_leg,
    set_spread_log_path,
    _load_spread_stats,
)


class TestSlippageAdaptive(unittest.TestCase):
    def test_fallback_when_file_missing(self):
        # Point to non-existent CSV
        set_spread_log_path(Path("/tmp/non_existent_spread_samples.csv"))
        slip = adaptive_slippage_per_leg("CRUDEOILM", 0.5)
        self.assertEqual(slip, 0.5)

    def test_fallback_when_insufficient_samples(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv") as tmp:
            writer = csv.writer(tmp)
            writer.writerow(["timestamp", "symbol", "bid", "ask", "spread", "mid", "spread_pct"])
            # Write only 5 samples (< MIN_SAMPLES=20)
            for _ in range(5):
                writer.writerow(["2026-09-22T10:00:00", "GOLDM", "75000", "75010", "10.0", "75005", "0.013"])
            tmp_path = Path(tmp.name)

        try:
            set_spread_log_path(tmp_path)
            slip = adaptive_slippage_per_leg("GOLDM", 0.5)
            self.assertEqual(slip, 0.5)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
