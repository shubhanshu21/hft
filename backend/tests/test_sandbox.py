"""Upstox sandbox rehearsal: correct payloads, honest reporting, and -- above all -- it can never change paper trading."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from upstox_client.rest import ApiException

from engine import sandbox_probe, sandbox_rehearsal
from services.broker import sandbox_client


class FakeApi:
    """Stands in for OrderApiV3 (sandbox)."""

    def __init__(self, behaviour="ok", body=None):
        self.behaviour, self.body, self.sent = behaviour, body, []

    def place_order(self, req):
        self.sent.append(req)
        if self.behaviour == "reject":
            exc = ApiException(status=400, reason="Bad Request")
            exc.body = json.dumps(self.body or {"errors": [{"errorCode": "UDAPI100060", "message": "Quantity is not a multiple of the lot size"}]}).encode()
            raise exc
        if self.behaviour == "boom":
            raise TimeoutError("read timed out")
        return SimpleNamespace(data=SimpleNamespace(order_ids=["SBX123"]))


class TestClient(unittest.TestCase):
    def test_an_accepted_order_returns_the_mock_order_id(self):
        api = FakeApi()
        r = sandbox_client.place("NSE_EQ|INE002A01018", 5, "BUY", api=api)
        self.assertTrue(r["ok"])
        self.assertEqual((r["order_id"], r["http_status"]), ("SBX123", 200))
        sent = api.sent[0]
        self.assertEqual((sent.quantity, sent.product, sent.instrument_token, sent.order_type, sent.transaction_type), (5, "I", "NSE_EQ|INE002A01018", "MARKET", "BUY"))

    def test_a_rejection_carries_upstoxs_own_code_and_text(self):
        r = sandbox_client.place("MCX_FO|1", 4, "BUY", api=FakeApi("reject"))
        self.assertFalse(r["ok"])
        self.assertEqual((r["http_status"], r["error_code"]), (400, "UDAPI100060"))
        self.assertIn("lot size", r["message"])

    def test_network_problems_never_raise(self):
        r = sandbox_client.place("K", 1, "BUY", api=FakeApi("boom"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "TimeoutError")

    def test_enabled_needs_a_token_and_can_be_switched_off(self):
        with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "", "SANDBOX_REHEARSAL": "true"}):
            self.assertFalse(sandbox_client.enabled())
        with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "tok", "SANDBOX_REHEARSAL": "true"}):
            self.assertTrue(sandbox_client.enabled())
        with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "tok", "SANDBOX_REHEARSAL": "false"}):
            self.assertFalse(sandbox_client.enabled())

    def _restore_default(self):
        import upstox_client
        saved = upstox_client.Configuration._default
        self.addCleanup(lambda: setattr(upstox_client.Configuration, "_default", saved))
        return upstox_client

    def test_the_sandbox_config_points_at_the_sandbox_host_even_after_the_live_config_exists(self):
        """The SDK caches the first Configuration and ignores later arguments: a naive Configuration(sandbox=True) returned the LIVE host in the daemon."""
        sdk = self._restore_default()
        sdk.Configuration._default = None
        live = sdk.Configuration()                                            # what UpstoxBroker does first: becomes the SDK's cached default
        self.assertNotIn("sandbox", live.host)
        naive = sdk.Configuration(sandbox=True)                               # the trap: constructor arguments are ignored
        self.assertNotIn("sandbox", naive.host)
        cfg = sandbox_client._config("tok")                                   # ours: genuinely sandbox
        self.assertIn("sandbox", cfg.host)
        self.assertIn("sandbox", cfg.order_host)
        self.assertEqual(cfg.access_token, "tok")

    def test_building_a_sandbox_config_never_turns_later_live_configs_into_sandbox_ones(self):
        sdk = self._restore_default()
        sdk.Configuration._default = None
        sandbox_client._config("tok")                                         # sandbox built FIRST
        later_live = sdk.Configuration()
        self.assertNotIn("sandbox", later_live.host)                          # a naive Configuration(sandbox=True) here would have poisoned this one
        self.assertNotEqual(later_live.access_token, "tok")

    def test_the_live_token_is_not_sent_to_the_sandbox(self):
        sdk = self._restore_default()
        sdk.Configuration._default = None
        live = sdk.Configuration()
        live.access_token = "LIVE-TOKEN"
        self.assertEqual(sandbox_client._config("SANDBOX-TOKEN").access_token, "SANDBOX-TOKEN")


class TestRehearsal(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "s.jsonl"
        p = patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "tok", "SANDBOX_REHEARSAL": "true"})
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def _place(self, ok=True):
        def place(key, qty, side, product, order_type, price, tag=""):
            self.calls.append((key, qty, side, product, order_type))
            return {"ok": ok, "order_id": "X" if ok else None, "http_status": 200 if ok else 400, "error_code": None if ok else "E1",
                    "message": "accepted" if ok else "nope", "latency_ms": 12}
        return place

    def test_it_is_off_without_a_token(self):
        with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": ""}):
            self.assertIsNone(sandbox_rehearsal.submit("entry", "CRUDEOILM", "commodity", "MCX_FO|1", "BUY", 3, 10, place=self._place(), path=self.path))
        self.assertEqual(self.calls, [])

    def test_it_sends_the_quantity_the_live_path_would_send(self):
        for market, sym, key, lots, mult in (("commodity", "CRUDEOILM", "MCX_FO|1", 3, 10), ("currency", "USDINR", "NCD_FO|2", 4, 1000), ("equity", "TATASTEEL", "NSE_EQ|3", 2500, 1)):
            sandbox_rehearsal.submit("entry", sym, market, key, "BUY", lots, mult, place=self._place(), path=self.path, wait=True)
        self.assertEqual([c[1] for c in self.calls], [3, 4000, 2500])                    # lots for MCX, units for currency and equity: engine/order_units.py
        self.assertTrue(all(c[3] == "I" and c[4] == "MARKET" for c in self.calls))

    def test_every_verdict_is_recorded_and_summarised(self):
        sandbox_rehearsal.submit("entry", "CRUDEOILM", "commodity", "MCX_FO|1", "BUY", 3, 10, "P1", place=self._place(True), path=self.path, wait=True)
        sandbox_rehearsal.submit("exit", "CRUDEOILM", "commodity", "MCX_FO|1", "SELL", 3, 10, "P1", place=self._place(False), path=self.path, wait=True)
        rows = sandbox_rehearsal.read(path=self.path)
        self.assertEqual([(r["kind"], r["ok"]) for r in rows], [("entry", True), ("exit", False)])
        s = sandbox_rehearsal.summarize(rows)
        self.assertEqual((s["total"], s["accepted"], s["rejected"]), (2, 1, 1))
        self.assertIn("E1: nope", s["by_reason"][0]["reason"])

    def test_a_missing_instrument_key_or_zero_quantity_sends_nothing(self):
        self.assertIsNone(sandbox_rehearsal.submit("entry", "X", "commodity", None, "BUY", 3, 10, place=self._place(), path=self.path))
        self.assertIsNone(sandbox_rehearsal.submit("entry", "X", "commodity", "K", "BUY", 0, 10, place=self._place(), path=self.path))
        self.assertEqual(self.calls, [])

    def test_a_crashing_sandbox_call_is_recorded_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("sandbox exploded")
        r = sandbox_rehearsal.submit("entry", "X", "commodity", "K", "BUY", 3, 10, place=boom, path=self.path, wait=True)
        self.assertFalse(r["ok"])
        self.assertEqual(sandbox_rehearsal.read(path=self.path)[0]["error_code"], "RuntimeError")


class TestProbe(unittest.TestCase):
    def test_the_matrix_covers_lots_and_units_for_every_traded_contract(self):
        seen = {}
        def place(key, qty, side, product, order_type, price, tag=""):
            seen.setdefault(key, set()).add(qty)
            ok = not (key == "NCD_FO|1" and qty == 4000)                       # pretend Upstox rejects one shape
            return {"ok": ok, "order_id": "X", "http_status": 200 if ok else 400, "error_code": None if ok else "UDAPI1", "message": "accepted" if ok else "bad qty"}
        keys = {"RELIANCE": "NSE_EQ|1", "USDINR": "NCD_FO|1", "CRUDEOILM": "MCX_FO|1", "SILVERMIC": "MCX_FO|2", "GOLDM": "MCX_FO|3"}
        rows = sandbox_probe.run(place=place, keys=keys)
        self.assertEqual(seen["NCD_FO|1"], {1, 1000, 4000})                    # lots AND units, per market
        self.assertEqual(seen["MCX_FO|1"], {1, 10, 100})
        text = sandbox_probe.format_rows(rows)
        self.assertIn("REJECTED [UDAPI1] bad qty", text)
        self.assertIn("qty  4000  MARKET", text)

    def test_an_unresolved_instrument_is_reported_not_crashed(self):
        rows = sandbox_probe.run(place=lambda *a, **k: {}, keys={})
        self.assertTrue(all(r["quantity"] is None for r in rows))


@unittest.skipUnless(os.environ.get("RUN_REPLAY"), "slow: real scan loop; set RUN_REPLAY=1")
class TestASandboxThatFailsCannotChangePaperTrading(unittest.TestCase):
    def test_paper_trades_are_identical_with_a_rejecting_and_a_raising_sandbox(self):
        import tests.replay_harness as h
        golden = json.loads((Path(__file__).parent / "golden" / "replay_quick.json").read_text())["trades"]
        key = lambda t: (t["symbol"], t["direction"], t["entry_dt"], t["exit_dt"], t["exit_reason"], t["qty"], t["net_pnl"])
        calls = []
        n = {"i": 0}

        def flaky(k, qty, side, product, order_type, price, tag=""):
            calls.append((k, qty, side))
            n["i"] += 1
            if n["i"] % 3 == 0:
                raise RuntimeError("sandbox down")
            return {"ok": n["i"] % 3 != 1, "order_id": None, "http_status": 400, "error_code": "E", "message": "rejected"}
        with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "tok", "SANDBOX_REHEARSAL": "true"}), patch.object(sandbox_client, "place", flaky), \
                patch.object(sandbox_rehearsal, "PATH", Path(tempfile.mkdtemp()) / "s.jsonl"):
            out = h.run_replay(days=h.QUICK_DAYS, instruments=h.QUICK_INSTRUMENTS)
            sandbox_rehearsal._executor().submit(lambda: None).result(timeout=30)          # drain the background worker
        self.assertEqual([key(t) for t in out["trades"]], [key(t) for t in golden])         # not one paper trade changed
        self.assertGreaterEqual(len(calls), 2 * len(out["trades"]))                          # every entry and exit was rehearsed


if __name__ == "__main__":
    unittest.main()
