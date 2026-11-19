"""engine/chromium_update.py -- Chromium is held since 2026-11-19 (a snap auto-refresh broke the Upstox login); this monthly job updates
it, tests a real login, reverts if the login breaks, and always holds it again. snap / login / Telegram are faked."""
import unittest

from engine import chromium_update as C


class Snap:
    def __init__(self, versions):
        self.v, self.calls = list(versions), []     # version shown before refresh, after refresh, after revert

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "refresh" and args[1:] == ("chromium",) and len(self.v) > 1:
            self.v.pop(0)
        if args[0] == "revert":
            self.v = [self.old]
        if args[0] == "list":
            return 0, f"Name Version Rev\nchromium {self.v[0]} 1"
        return 0, ""


class TestUpdate(unittest.TestCase):
    def go(self, snap, logins):
        sent, it = [], iter(logins)
        snap.old = snap.v[0]
        code = C.run(sh=snap, login=lambda: next(it), send=sent.append, log=lambda *a: None)
        self.assertEqual(snap.calls[-1], ("refresh", "--hold", "chromium"))     # always held again
        return code, sent

    def test_update_that_works_is_kept(self):
        snap = Snap(["157", "158"])
        code, sent = self.go(snap, [True])
        self.assertEqual(code, 0)
        self.assertNotIn(("revert", "chromium"), snap.calls)
        self.assertIn("157 → 158", sent[0])

    def test_update_that_breaks_the_login_is_reverted(self):
        snap = Snap(["157", "158"])
        code, sent = self.go(snap, [False, True])
        self.assertEqual(code, 0)
        self.assertIn(("revert", "chromium"), snap.calls)
        self.assertIn("put back to 157", sent[0])

    def test_no_update_and_working_login_says_nothing(self):
        code, sent = self.go(Snap(["157"]), [True])
        self.assertEqual((code, sent), (0, []))

    def test_login_broken_without_an_update_is_a_problem(self):
        snap = Snap(["157"])
        code, sent = self.go(snap, [False])
        self.assertEqual(code, 1)
        self.assertNotIn(("revert", "chromium"), snap.calls)
        self.assertIn("not the update", sent[0])
