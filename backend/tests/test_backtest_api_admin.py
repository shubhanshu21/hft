"""Changing saved data from the website needs no token since 2026-11-18 (user: "remove this token thing i dont want it"; the site has
no login since 2026-10-31). Delete, rename and the paper reset work with no header and no BACKTEST_ADMIN_TOKEN; a stray old header is
ignored. Reading stays open, and finishing a run never deletes older saved results."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from engine import backtest_api as A


class TestNoToken(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "abc123abc123.json").write_text(json.dumps({"id": "abc123abc123", "status": "done"}))
        patcher = mock.patch.object(A, "RUNS_DIR", self.tmp)
        patcher.start(); self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("BACKTEST_ADMIN_TOKEN", None)
        self.srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self, path, method, data=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_rename_needs_no_token(self):
        self.assertEqual(self.call("/api/backtests/abc123abc123/label", "POST", json.dumps({"label": "mine"}).encode()), HTTPStatus.OK)
        self.assertEqual(json.loads((self.tmp / "abc123abc123.json").read_text())["label"], "mine")

    def test_delete_needs_no_token_and_ignores_an_old_header(self):
        self.assertEqual(self.call("/api/backtests/abc123abc123", "DELETE", headers={"X-API-Token": "left over in a browser"}), HTTPStatus.OK)
        self.assertFalse((self.tmp / "abc123abc123.json").exists())

    def test_reading_runs_stays_open(self):
        self.assertEqual(self.call("/api/backtests", "GET"), HTTPStatus.OK)


if __name__ == "__main__":
    unittest.main()


class TestSavedRunsAreKept(unittest.TestCase):
    def test_finishing_a_run_deletes_no_older_results(self):
        """2026-10-30 audit: finishing a backtest silently deleted saved results beyond the newest 200 -- against the no-delete rule."""
        tmp = Path(tempfile.mkdtemp())
        for i in range(205):
            (tmp / f"{i:012x}.json").write_text(json.dumps({"id": f"{i:012x}", "status": "done"}))
        run = {"id": "ffffffffffff", "status": "queued", "market": "nosuch", "strategy": "nosuch", "params": {}}
        with mock.patch.object(A, "RUNS_DIR", tmp):
            A.work(run)                                              # fails fast (unknown strategy) but still finishes and saves
        self.assertEqual(len(list(tmp.glob("*.json"))), 206)
