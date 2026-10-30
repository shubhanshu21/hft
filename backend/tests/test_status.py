"""`cli.py status` (engine/status.py). 2026-10-30 audit: it reported hft-dryrun.service and its heartbeat file, both removed 2026-10-21,
so it always said "inactive / no heartbeat" and never looked at the real engines."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from engine import status as S
from engine.database import TradingDB


class TestStatus(unittest.TestCase):
    def test_shows_the_real_engine_not_the_removed_daemon(self):
        db = Path(tempfile.mkdtemp()) / "t.db"
        TradingDB(db).init_account("RESULTS_GAP", capital=100_000.0)
        live = {"ok": True, "text": "Running · last check Fri 09:30 IST · next at Fri 09:40 IST"}
        with mock.patch("engine.engine_monitor.engine_status", return_value=live) as es:
            out = S.render(S.collect("RESULTS_GAP", db_path=db, now=datetime(2026, 10, 30, 10, 0, tzinfo=S.IST)))
        es.assert_called_once_with("RESULTS_GAP")
        self.assertIn("Engine      OK   Running · last check Fri 09:30 IST", out)
        self.assertNotIn("Daemon", out)
        self.assertNotIn("heartbeat", out)
        self.assertIn("Capital     Rs100,000.00", out)


if __name__ == "__main__":
    unittest.main()
