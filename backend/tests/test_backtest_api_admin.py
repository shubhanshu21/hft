"""The destructive API calls must not be reachable by an anonymous caller on the public port (2026-10-26).

DELETE /api/backtests[/<id>] and POST /api/paper/reset are refused unless the request carries an X-API-Token matching
BACKTEST_ADMIN_TOKEN; with no token configured they are refused outright. Reading and starting a run stay open.
"""
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


class TestAdminCheck(unittest.TestCase):
    def test_no_token_configured_refuses_everything(self):
        self.assertEqual(A.admin_check("", expected=""), HTTPStatus.FORBIDDEN)
        self.assertEqual(A.admin_check("anything", expected=""), HTTPStatus.FORBIDDEN)

    def test_missing_or_wrong_token_is_unauthorised(self):
        self.assertEqual(A.admin_check("", expected="s3cret"), HTTPStatus.UNAUTHORIZED)
        self.assertEqual(A.admin_check("s3cret2", expected="s3cret"), HTTPStatus.UNAUTHORIZED)

    def test_right_token_passes_and_is_whitespace_tolerant(self):
        self.assertIsNone(A.admin_check("s3cret", expected="s3cret"))
        self.assertIsNone(A.admin_check("  s3cret\t", expected="s3cret"))

    def test_reads_the_env_when_no_expectation_is_given(self):
        with mock.patch.dict(os.environ, {A.ADMIN_TOKEN_ENV: "fromenv"}):
            self.assertIsNone(A.admin_check("fromenv"))
            self.assertEqual(A.admin_check("other"), HTTPStatus.UNAUTHORIZED)


class TestDestructiveRoutesRefuseAnonymousCallers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "abc123abc123.json").write_text(json.dumps({"id": "abc123abc123", "status": "done"}))
        patcher = mock.patch.object(A, "RUNS_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(A.ADMIN_TOKEN_ENV, None)
        self.srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def _delete(self, path, token=None):
        req = urllib.request.Request(self.base + path, method="DELETE")
        if token:
            req.add_header("X-API-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_delete_is_refused_when_no_token_is_configured(self):
        self.assertEqual(self._delete("/api/backtests/abc123abc123"), HTTPStatus.FORBIDDEN)
        self.assertTrue((self.tmp / "abc123abc123.json").exists(), "the run must survive a refused delete")

    def test_delete_needs_the_right_token(self):
        os.environ[A.ADMIN_TOKEN_ENV] = "s3cret"
        self.assertEqual(self._delete("/api/backtests/abc123abc123", "nope"), HTTPStatus.UNAUTHORIZED)
        self.assertTrue((self.tmp / "abc123abc123.json").exists(), "a wrong token must not delete anything")
        self.assertEqual(self._delete("/api/backtests/abc123abc123", "s3cret"), HTTPStatus.OK)
        self.assertFalse((self.tmp / "abc123abc123.json").exists())

    def test_reading_runs_stays_open(self):
        with urllib.request.urlopen(self.base + "/api/backtests", timeout=5) as r:
            self.assertEqual(r.status, HTTPStatus.OK)


if __name__ == "__main__":
    unittest.main()
