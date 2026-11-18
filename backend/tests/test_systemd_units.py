"""deploy/systemd: every Python service prints straight to the journal (PYTHONUNBUFFERED, 2026-11-18) -- before, the 6-hour breakout
session's lines only appeared when it finished."""
import unittest
from pathlib import Path

UNITS = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


class TestUnits(unittest.TestCase):
    def test_every_service_is_unbuffered(self):
        services = sorted(UNITS.glob("*.service"))
        self.assertTrue(services)
        for f in services:
            with self.subTest(unit=f.name):
                self.assertIn("Environment=PYTHONUNBUFFERED=1", f.read_text().split("[Service]", 1)[1])


if __name__ == "__main__":
    unittest.main()
