"""Backups must be restorable: snapshot, verify, prune, and a restore that can itself be undone."""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from engine import backup_db


def _make_db(path: Path, trades=3):
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE accounts(id TEXT); CREATE TABLE positions(id TEXT); CREATE TABLE orders(id TEXT); CREATE TABLE trades(id INTEGER);")
    con.execute("INSERT INTO accounts VALUES ('A')")
    con.executemany("INSERT INTO trades VALUES (?)", [(i,) for i in range(trades)])
    con.commit()
    con.close()


class TestBackups(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db, self.dir = self.tmp / "live.db", self.tmp / "backups"
        _make_db(self.db)

    def _count(self, path):
        con = sqlite3.connect(str(path))
        try:
            return con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        finally:
            con.close()

    def test_daily_and_hourly_snapshots_have_distinct_names_and_contents(self):
        d = backup_db.backup_once(self.db, self.dir, "daily", datetime(2026, 9, 24, 0, 30))
        h = backup_db.backup_once(self.db, self.dir, "hourly", datetime(2026, 9, 24, 14, 0))
        self.assertEqual((d.name, h.name), ("paper_trading_2026-09-24.db", "hourly_2026-09-24_1400.db"))
        self.assertEqual(self._count(d), 3)

    def test_pruning_keeps_the_newest_of_each_kind_and_never_touches_the_other_kind(self):
        for day in range(1, 21):
            backup_db.backup_once(self.db, self.dir, "daily", datetime(2026, 9, day))
        for hour in range(24):
            backup_db.backup_once(self.db, self.dir, "hourly", datetime(2026, 9, 24, hour))
        backup_db.backup_once(self.db, self.dir, "hourly", datetime(2026, 9, 25, 5))
        backup_db.backup_once(self.db, self.dir, "pre_reset", datetime(2026, 9, 24, 1))
        self.assertEqual(len(list(self.dir.glob("paper_trading_*.db"))), backup_db.KEEP_DAILY)
        self.assertEqual(len(list(self.dir.glob("hourly_*.db"))), 25)             # under KEEP_HOURLY: none pruned
        self.assertEqual(len(list(self.dir.glob("pre_reset_*.db"))), 1)            # manual safety copies are never pruned
        self.assertTrue((self.dir / "paper_trading_2026-09-20.db").exists())
        self.assertFalse((self.dir / "paper_trading_2026-09-06.db").exists())

    def test_verify_counts_rows_and_rejects_garbage(self):
        b = backup_db.backup_once(self.db, self.dir)
        self.assertEqual(backup_db.verify_backup(b)["trades"], 3)
        bad = self.dir / "bad.db"
        bad.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(ValueError):
            backup_db.verify_backup(bad)
        other = self.dir / "other.db"
        sqlite3.connect(str(other)).executescript("CREATE TABLE t(x);")
        with self.assertRaises(ValueError):
            backup_db.verify_backup(other)                                          # a valid SQLite file that is not our DB

    def test_restore_replaces_the_live_db_and_keeps_the_old_one(self):
        b = backup_db.backup_once(self.db, self.dir)
        con = sqlite3.connect(str(self.db))
        con.execute("DELETE FROM trades")                                           # the accident
        con.commit()
        con.close()
        self.assertEqual(self._count(self.db), 0)
        saved = backup_db.restore(b, self.db, self.dir)
        self.assertEqual(self._count(self.db), 3)                                   # history is back
        self.assertEqual(self._count(saved), 0)                                     # and the pre-restore state is itself recoverable
        self.assertTrue(saved.name.startswith("pre_restore_"))

    def test_a_bad_backup_never_overwrites_a_good_db(self):
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"garbage" * 1000)
        with self.assertRaises(ValueError):
            backup_db.restore(bad, self.db, self.dir)
        self.assertEqual(self._count(self.db), 3)

    def test_restore_drops_a_stale_wal_from_the_old_database(self):
        b = backup_db.backup_once(self.db, self.dir)
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"stale")
        backup_db.restore(b, self.db, self.dir)
        self.assertFalse(wal.exists())

    def test_backup_of_a_missing_db_is_a_noop(self):
        self.assertIsNone(backup_db.backup_once(self.tmp / "nope.db", self.dir))

    def test_labeled_backups_for_a_second_database_dont_collide_with_or_get_pruned_by_the_first(self):
        crypto_db = self.tmp / "crypto.db"
        con = sqlite3.connect(str(crypto_db))
        con.executescript("CREATE TABLE state(k TEXT); CREATE TABLE holdings(k TEXT); CREATE TABLE trades(id INTEGER); CREATE TABLE equity(id INTEGER);")
        con.execute("INSERT INTO equity VALUES (1)")
        con.commit()
        con.close()

        d1 = backup_db.backup_once(self.db, self.dir, "daily", datetime(2026, 9, 24), label=None)
        d2 = backup_db.backup_once(crypto_db, self.dir, "daily", datetime(2026, 9, 24), label="crypto_paper")
        h2 = backup_db.backup_once(crypto_db, self.dir, "hourly", datetime(2026, 9, 24, 14, 0), label="crypto_paper")
        self.assertEqual((d1.name, d2.name, h2.name), ("paper_trading_2026-09-24.db", "crypto_paper_2026-09-24.db", "crypto_paper_hourly_2026-09-24_1400.db"))

        # pruning one database's daily backups never touches the other's (daily or hourly)
        for day in range(1, 21):
            backup_db.backup_once(self.db, self.dir, "daily", datetime(2026, 9, day), label=None)
            backup_db.backup_once(crypto_db, self.dir, "daily", datetime(2026, 9, day), label="crypto_paper")
        self.assertEqual(len(list(self.dir.glob("paper_trading_*.db"))), backup_db.KEEP_DAILY)
        self.assertEqual(len(list(self.dir.glob("crypto_paper_[0-9]*.db"))), backup_db.KEEP_DAILY)
        self.assertTrue(h2.exists())                                                 # the earlier hourly snapshot survived the daily prune

        # verify_backup against crypto's own schema (no "accounts" table -- see DATABASES)
        counts = backup_db.verify_backup(d2, backup_db.CRYPTO_TABLES, "equity")
        self.assertEqual(counts["equity"], 1)
        with self.assertRaises(ValueError):
            backup_db.verify_backup(d2)                                             # main DB's schema check correctly rejects a crypto backup

    def test_restore_targets_the_given_databases_schema(self):
        crypto_db = self.tmp / "crypto.db"
        con = sqlite3.connect(str(crypto_db))
        con.executescript("CREATE TABLE state(k TEXT); CREATE TABLE holdings(k TEXT); CREATE TABLE trades(id INTEGER); CREATE TABLE equity(id INTEGER);")
        con.execute("INSERT INTO equity VALUES (1)")
        con.commit()
        con.close()
        b = backup_db.backup_once(crypto_db, self.dir, "daily", label="crypto_paper")

        con = sqlite3.connect(str(crypto_db))
        con.execute("DELETE FROM equity")
        con.commit()
        con.close()

        backup_db.restore(b, crypto_db, self.dir, backup_db.CRYPTO_TABLES, "equity")
        con = sqlite3.connect(str(crypto_db))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM equity").fetchone()[0], 1)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
