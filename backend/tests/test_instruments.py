"""services/broker/instruments.py -- symbol -> Upstox instrument_key resolution.

This is what turns "RELIANCE" or "SILVER" into the key a live order/quotes call is made with, and it has silently resolved to a
wrong contract twice (SILVER100 = 100x the price scale; an expired key kept forever by a long-running process). No network here:
the github-style CSV masters are written to a temp dir.
"""
import csv
import gzip
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from services.broker import instruments as ins

FIELDS = ["tradingsymbol", "instrument_key", "exchange", "instrument_type", "expiry", "name", "lot_size"]


def _row(tsym, key, exchange, itype, expiry="2099-01-01", name=""):
    return {"tradingsymbol": tsym, "instrument_key": key, "exchange": exchange, "instrument_type": itype, "expiry": expiry, "name": name, "lot_size": "1"}


def _write_gz(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _future(days=30):
    return (date.today() + timedelta(days=days)).isoformat()


def _past(days=30):
    return (date.today() - timedelta(days=days)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.addCleanup(mock.patch.stopall)
        for name, value in (("_CACHE_DIR", d), ("_CACHE_FILE", d / "nse.csv.gz"), ("_META_FILE", d / "nse_meta.json"),
                            ("_MCX_CACHE_FILE", d / "mcx.csv.gz"), ("_MCX_META_FILE", d / "mcx_meta.json")):
            mock.patch.object(ins, name, value).start()
        # module-level maps outlive a test: reset them so one case can't leak a key into the next
        for name in ("_SYMBOL_KEY_MAP", "_MCX_KEY_MAP", "_CURRENCY_KEY_MAP", "_INDEX_FUT_KEY_MAP", "_MCX_EXPIRY"):
            mock.patch.object(ins, name, {}).start()
        mock.patch.object(ins, "_last_parsed_date", None).start()
        # offline by default: no test here is allowed to touch the network. Tests that care about a download override these.
        mock.patch.object(ins, "_download_master", side_effect=OSError("offline")).start()
        mock.patch.object(ins, "_download_mcx_master", side_effect=OSError("offline")).start()

    def write_nse(self, rows, meta_date=None):
        _write_gz(ins._CACHE_FILE, rows)
        ins._META_FILE.write_text(json.dumps({"date": meta_date or date.today().isoformat()}))

    def write_mcx(self, rows, meta_date=None):
        _write_gz(ins._MCX_CACHE_FILE, rows)
        ins._MCX_META_FILE.write_text(json.dumps({"date": meta_date or date.today().isoformat()}))


class TestCacheFreshness(Base):
    def test_todays_meta_is_fresh(self):
        self.write_nse([])
        self.assertTrue(ins._cache_is_fresh(ins._META_FILE, ins._CACHE_FILE))

    def test_yesterdays_meta_is_not(self):
        self.write_nse([], meta_date=_past(1))
        self.assertFalse(ins._cache_is_fresh(ins._META_FILE, ins._CACHE_FILE))

    def test_a_missing_or_corrupt_meta_is_not_fresh(self):
        _write_gz(ins._CACHE_FILE, [])
        self.assertFalse(ins._cache_is_fresh(ins._META_FILE, ins._CACHE_FILE))     # meta missing
        ins._META_FILE.write_text("{not json")
        self.assertFalse(ins._cache_is_fresh(ins._META_FILE, ins._CACHE_FILE))
        ins._META_FILE.write_text(json.dumps({"date": date.today().isoformat()}))
        ins._CACHE_FILE.unlink()
        self.assertFalse(ins._cache_is_fresh(ins._META_FILE, ins._CACHE_FILE))     # cache missing


class TestNseMaster(Base):
    def test_only_nse_eq_equity_rows_are_indexed(self):
        self.write_nse([_row("RELIANCE", "NSE_EQ|INE002A01018", "NSE_EQ", "EQUITY"),
                        _row("USDINR26SEPFUT", "NCD_FO|1", "NCD_FO", "FUTCUR"),
                        _row("NIFTY26SEPFUT", "NSE_FO|2", "NSE_FO", "FUTIDX"),
                        _row("SOMEBOND", "NSE_EQ|3", "NSE_EQ", "DEBENTURE")])
        ins._load_master()
        self.assertEqual(ins._SYMBOL_KEY_MAP, {"RELIANCE": "NSE_EQ|INE002A01018"})


class TestMcxMaster(Base):
    def test_nearest_expiry_wins_and_expired_contracts_are_ignored(self):
        self.write_mcx([_row("CRUDEOILM26NOVFUT", "MCX|old", "MCX_COMM", "FUTCOM", expiry=_past(1)),
                        _row("CRUDEOILM26DECFUT", "MCX|mid", "MCX_COMM", "FUTCOM", expiry=_future(20)),
                        _row("CRUDEOILM27JANFUT", "MCX|far", "MCX_COMM", "FUTCOM", expiry=_future(60))])
        ins._load_mcx_master()
        self.assertEqual(ins._MCX_KEY_MAP["CRUDEOILM"], "MCX|mid")
        self.assertEqual(ins._MCX_EXPIRY["CRUDEOILM"], _future(20))

    def test_the_silver100_series_is_never_selected_for_silver(self):
        # 2026-09-23 live bug: SILVER100 (100x smaller denomination, lot 100) expired sooner, so nearest-expiry picked it and
        # every recorded SILVER price was off by ~100x. It also must not steal SILVERMIC/SILVERM.
        self.write_mcx([_row("SILVER10026DECFUT", "MCX|wrong", "MCX_COMM", "FUTCOM", expiry=_future(10)),
                        _row("SILVER26DECFUT", "MCX|silver", "MCX_COMM", "FUTCOM", expiry=_future(20)),
                        _row("SILVERMIC26DECFUT", "MCX|mic", "MCX_COMM", "FUTCOM", expiry=_future(10)),
                        _row("SILVERM26DECFUT", "MCX|mini", "MCX_COMM", "FUTCOM", expiry=_future(10))])
        ins._load_mcx_master()
        self.assertEqual(ins._MCX_KEY_MAP["SILVER"], "MCX|silver")
        self.assertEqual(ins._MCX_KEY_MAP["SILVERMIC"], "MCX|mic")
        self.assertEqual(ins._MCX_KEY_MAP["SILVERM"], "MCX|mini")

    def test_longer_prefixes_are_matched_before_the_shorter_ones_they_start_with(self):
        self.write_mcx([_row("CRUDEOIL26DECFUT", "MCX|crude", "MCX_COMM", "FUTCOM", expiry=_future(20)),
                        _row("CRUDEOILM26DECFUT", "MCX|crudemini", "MCX_COMM", "FUTCOM", expiry=_future(20))])
        ins._load_mcx_master()
        self.assertEqual(ins._MCX_KEY_MAP["CRUDEOIL"], "MCX|crude")
        self.assertEqual(ins._MCX_KEY_MAP["CRUDEOILM"], "MCX|crudemini")


class TestCurrencyMaster(Base):
    def test_the_monthly_contract_is_preferred_over_the_weekly(self):
        # Weekly expiries expire in days with no usable history; only the monthly pattern is traded/backtested.
        self.write_nse([_row("USDINR26918FUT", "NCD_FO|weekly", "NCD_FO", "FUTCUR", expiry=_future(5)),
                        _row("USDINR26SEPFUT", "NCD_FO|monthly", "NCD_FO", "FUTCUR", expiry=_future(40)),
                        _row("EURINR26SEPFUT", "NCD_FO|eur", "NCD_FO", "FUTCUR", expiry=_future(40)),
                        _row("JPYINR26SEPFUT", "NCD_FO|jpy", "NCD_FO", "FUTCUR", expiry=_future(40))])
        ins._load_currency_master()
        self.assertEqual(ins._CURRENCY_KEY_MAP, {"USDINR": "NCD_FO|monthly", "EURINR": "NCD_FO|eur"})   # only the three we trade


class TestIndexFutures(Base):
    def test_only_nifty_and_banknifty_are_resolved(self):
        self.write_nse([_row("NIFTY26SEPFUT", "NSE_FO|nifty", "NSE_FO", "FUTIDX", expiry=_future(20), name="NIFTY"),
                        _row("BANKNIFTY26SEPFUT", "NSE_FO|bank", "NSE_FO", "FUTIDX", expiry=_future(20), name="BANKNIFTY"),
                        _row("FINNIFTY26SEPFUT", "NSE_FO|fin", "NSE_FO", "FUTIDX", expiry=_future(20), name="FINNIFTY")])
        ins._load_index_futures_master()
        self.assertEqual(ins._INDEX_FUT_KEY_MAP, {"NIFTY": "NSE_FO|nifty", "BANKNIFTY": "NSE_FO|bank"})


class TestEnsureMaster(Base):
    ROWS = [_row("RELIANCE", "NSE_EQ|INE002A01018", "NSE_EQ", "EQUITY")]

    def test_a_fresh_cache_is_parsed_without_a_download(self):
        self.write_nse(self.ROWS)
        with mock.patch.object(ins, "_download_master", side_effect=AssertionError("must not hit the network")):
            ins.ensure_master()
        self.assertEqual(ins._SYMBOL_KEY_MAP, {"RELIANCE": "NSE_EQ|INE002A01018"})
        self.assertEqual(ins._last_parsed_date, date.today().isoformat())

    def test_a_stale_cache_is_downloaded_once_and_reparsed(self):
        self.write_nse(self.ROWS, meta_date=_past(1))
        with mock.patch.object(ins, "_download_master") as dl:
            ins.ensure_master()
        dl.assert_called_once_with()

    def test_a_failed_download_falls_back_to_the_cached_master(self):
        # A network blip must not take the 09:30 run down: yesterday's master is on disk and still almost certainly right.
        self.write_nse(self.ROWS, meta_date=_past(1))
        with mock.patch.object(ins, "_download_master", side_effect=OSError("no network")):
            ins.ensure_master()
        self.assertEqual(ins.get_instrument_key("RELIANCE"), "NSE_EQ|INE002A01018")

    def test_no_download_and_no_cache_leaves_everything_unresolved_instead_of_raising(self):
        with mock.patch.object(ins, "_download_master", side_effect=OSError("no network")):
            ins.ensure_master()
        self.assertIsNone(ins.get_instrument_key("RELIANCE"))

    def test_the_master_is_reparsed_once_per_process_per_day_not_on_every_lookup(self):
        self.write_nse(self.ROWS)
        ins.ensure_master()
        with mock.patch.object(ins, "_load_master", side_effect=AssertionError("reparsed unnecessarily")):
            ins.get_instrument_key("RELIANCE")
            ins.get_instrument_key("RELIANCE")

    def test_a_second_lookup_can_still_resolve_new_symbols(self):
        self.write_nse(self.ROWS + [_row("TCS", "NSE_EQ|TCS", "NSE_EQ", "EQUITY")])
        self.assertEqual(ins.get_instrument_key("reliance"), "NSE_EQ|INE002A01018")
        self.assertEqual(ins.get_instrument_key("TCS"), "NSE_EQ|TCS")

    def test_the_master_is_reparsed_after_a_midnight_rollover_without_a_restart(self):
        # The 2026-10-01 bug: a daemon running for days kept the contracts it parsed on day one. A new calendar day in this
        # process must reparse even when someone else (the 00:30 top-up) is the one that downloaded the file.
        self.write_nse(self.ROWS)
        ins.ensure_master()

        def reparse():
            ins._SYMBOL_KEY_MAP.clear()
            ins._SYMBOL_KEY_MAP.update({"RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|TCS"})

        with mock.patch.object(ins, "_last_parsed_date", _past(1)), \
             mock.patch.object(ins, "_cache_is_fresh", return_value=True), \
             mock.patch.object(ins, "_load_master", side_effect=reparse) as load:
            ins.ensure_master()
        load.assert_called_once_with()
        self.assertEqual(ins._last_parsed_date, date.today().isoformat())
        self.assertEqual(ins.get_instrument_key("TCS", auto_refresh=False), "NSE_EQ|TCS")

    def test_a_failed_mcx_download_keeps_the_cached_mcx_contracts(self):
        self.write_nse(self.ROWS)
        self.write_mcx([_row("CRUDEOILM26DECFUT", "MCX|crude", "MCX_COMM", "FUTCOM", expiry=_future(20))], meta_date=_past(1))
        with mock.patch.object(ins, "_download_mcx_master", side_effect=OSError("no network")):
            ins.ensure_master()
        self.assertEqual(ins.get_instrument_key("CRUDEOILM"), "MCX|crude")


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ins, "ensure_master", return_value=None).start()      # never download in a lookup test
        mock.patch.object(ins, "_SYMBOL_KEY_MAP", {"RELIANCE": "EQ|1", "SHARED": "EQ|2"}).start()
        mock.patch.object(ins, "_MCX_KEY_MAP", {"CRUDEOILM": "MCX|1", "SHARED": "MCX|2"}).start()
        mock.patch.object(ins, "_CURRENCY_KEY_MAP", {"USDINR": "CUR|1"}).start()
        mock.patch.object(ins, "_INDEX_FUT_KEY_MAP", {"NIFTY": "IDX|1"}).start()

    def test_each_kind_of_symbol_resolves(self):
        for sym, key in (("RELIANCE", "EQ|1"), ("CRUDEOILM", "MCX|1"), ("USDINR", "CUR|1"), ("NIFTY", "IDX|1")):
            self.assertEqual(ins.get_instrument_key(sym, auto_refresh=False), key, sym)

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(ins.get_instrument_key("reliance", auto_refresh=False), "EQ|1")

    def test_an_unknown_symbol_returns_none_rather_than_a_guess(self):
        self.assertIsNone(ins.get_instrument_key("NOSUCHTHING", auto_refresh=False))

    def test_resolve_symbols_skips_unknowns(self):
        self.assertEqual(ins.resolve_symbols(["RELIANCE", "NOSUCHTHING"]), {"RELIANCE": "EQ|1"})


if __name__ == "__main__":
    unittest.main()
