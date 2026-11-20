"""2026-11-20: an engine's account name follows its code (the crypto account said '12-coin' while trading 10); a call without a name
(cli.py, print_dashboard) never renames an existing account."""
import tempfile
import unittest
from pathlib import Path

from engine.database import TradingDB


class AccountNameTest(unittest.TestCase):
    def setUp(self):
        self.db = TradingDB(str(Path(tempfile.mkdtemp()) / "t.db"))
        self.db.init_account("A", name="Old name (12-coin)", capital=1000.0)

    def test_a_named_call_updates_the_stored_name(self):
        self.db.init_account("A", name="New name (10-coin)", capital=1000.0)
        self.assertEqual(self.db.get_account("A")["name"], "New name (10-coin)")

    def test_a_call_without_a_name_keeps_it(self):
        self.db.init_account("A", capital=1000.0)
        self.assertEqual(self.db.get_account("A")["name"], "Old name (12-coin)")

    def test_a_new_account_without_a_name_gets_the_default(self):
        self.db.init_account("B")
        self.assertEqual(self.db.get_account("B")["name"], "Live Dry Run Scalper")

    def test_the_crypto_engine_names_its_real_coin_count(self):
        from engine import crypto_dip_reversal as E
        from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S
        E.init_account(self.db)
        self.assertIn(f"{len(S.COINS)}-coin", self.db.get_account(E.ACCOUNT_ID)["name"])


if __name__ == "__main__":
    unittest.main()
