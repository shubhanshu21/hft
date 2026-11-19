"""The headless Upstox login must cope with BOTH screens Upstox can show after the mobile number, and must leave evidence when it fails.

2026-09-25 09:00: the daemon waited 15 s for an OTP box that never appeared (Upstox recognised the browser after the 06:30 nightly login and went
straight to the PIN), the login failed, and the session opened without a token."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selenium.common.exceptions import NoSuchElementException

from services.auth import upstox_auto_login as m


class El:
    def __init__(self, driver, name):
        self.d, self.name, self.attrs = driver, name, {"id": name}

    def clear(self): pass
    def send_keys(self, text): self.d.typed.append((self.name, text))
    def click(self): self.d.click(self.name)
    def get_attribute(self, a): return self.attrs.get(a)
    @property
    def text(self): return "Upstox login page text"


class FakeDriver:
    """A tiny state machine of Upstox's login: mobile -> (otp | pin) -> pin -> redirect."""

    def __init__(self, after_mobile="otp"):
        self.state, self.after_mobile, self.typed, self.clicked, self.current_url, self.screenshots = "mobile", after_mobile, [], [], "https://login.upstox.com/", []

    def get(self, url): pass

    def find_element(self, by, value):
        wanted = [v.strip().lstrip("#") for v in value.split(",")] if by == "css selector" else [value]
        visible = {"mobile": ["mobileNum", "getOtp"], "otp": ["otpNum", "continueBtn"], "pin": ["pinCode", "pinContinueBtn"]}.get(self.state, [])
        for w in wanted:
            if w in visible:
                return El(self, w)
        if by == "tag name" and value == "body":
            return El(self, "body")
        raise NoSuchElementException(value)

    def click(self, name):
        self.clicked.append(name)
        if name == "getOtp":
            self.state = self.after_mobile
        elif name == "continueBtn":
            self.state = "pin"
        elif name == "pinContinueBtn":
            self.state, self.current_url = "done", "https://127.0.0.1/?code=THE_CODE"

    def execute_script(self, script, el):                 # _js_click
        el.click()

    def save_screenshot(self, path):
        self.screenshots.append(path)
        Path(path).write_bytes(b"png")

    def quit(self): pass


class TestLoginFlow(unittest.TestCase):
    def _run(self, driver):
        with patch.object(m, "_setup_driver", return_value=driver), patch.object(m.UpstoxConfig, "USERNAME", "9999999999"), \
                patch.object(m.UpstoxConfig, "PIN", "123456"), patch.object(m.UpstoxConfig, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"), \
                patch.object(m.UpstoxConfig, "API_KEY", "k"), patch.object(m.UpstoxConfig, "REDIRECT_URI", "https://127.0.0.1/"):
            return m._auto_login_get_code()

    def test_the_full_flow_with_an_otp_screen(self):
        d = FakeDriver("otp")
        self.assertEqual(self._run(d), "THE_CODE")
        self.assertEqual([t[0] for t in d.typed], ["mobileNum", "otpNum", "pinCode"])           # mobile, TOTP, PIN

    def test_when_upstox_skips_the_otp_and_goes_straight_to_the_pin_the_login_still_succeeds(self):
        """The 2026-09-25 09:00 failure: no OTP box ever appears after the mobile number."""
        d = FakeDriver("pin")
        self.assertEqual(self._run(d), "THE_CODE")
        self.assertEqual([t[0] for t in d.typed], ["mobileNum", "pinCode"])                     # no OTP typed
        self.assertNotIn("continueBtn", d.clicked)

    def test_a_failure_leaves_a_screenshot_and_the_page_text_behind(self):
        d = FakeDriver("neither")                                                              # after the mobile number nothing recognisable appears
        tmp = Path(tempfile.mkdtemp())
        with patch("core.paths.LOG_DIR", tmp), patch("selenium.webdriver.support.wait.WebDriverWait.until", side_effect=TimeoutError("no screen")):
            with self.assertRaises(Exception):
                self._run(d)
        self.assertTrue(list(tmp.glob("login_failure_*.png")))
        self.assertIn("Upstox login page text", next(tmp.glob("login_failure_*.txt")).read_text())

    def test_the_failure_log_names_the_exception_type_when_its_message_is_empty(self):
        import logging
        with patch.object(m.UpstoxConfig, "auto_login_configured", return_value=True), patch.object(m.UpstoxConfig, "ACCESS_TOKEN", "old"), \
                patch.object(m, "_token_is_valid", return_value=False), patch.object(m.UpstoxConfig, "cached_token", return_value=""), \
                patch.object(m, "_auto_login_get_code", side_effect=TimeoutError()), self.assertLogs(m.log, level=logging.CRITICAL) as cm:
            self.assertIsNone(m.ensure_fresh_upstox_token())
        self.assertIn("TimeoutError", cm.output[0])
        self.assertIn("no message", cm.output[0])


if __name__ == "__main__":
    unittest.main()


class TestSnapDriverChoice(unittest.TestCase):
    """2026-11-19: snap Chromium 157's bundled chromedriver needs GLIBC 2.42; run directly it died at start and the morning had no token.
    The login now tries driver/browser pairs in order: the snap's launcher (with the in-snap browser -- it cannot exec /snap/bin),
    the bundled driver, then Selenium Manager -- and moves on when one will not start."""

    def test_snap_order(self):
        with patch.object(m.Path, "exists", lambda p: str(p) in (m._SNAP_DRIVER_LAUNCHER, m._SNAP_CHROMIUM_DRIVER)):
            got = m._driver_choices(m._SNAP_CHROMIUM_BINARY)
        self.assertEqual(got, [(m._SNAP_DRIVER_LAUNCHER, m._SNAP_CHROME_INSIDE), (m._SNAP_CHROMIUM_DRIVER, m._SNAP_CHROMIUM_BINARY),
                               (None, m._SNAP_CHROMIUM_BINARY)])
        with patch.object(m.Path, "exists", lambda p: False):
            self.assertEqual(m._driver_choices(m._SNAP_CHROMIUM_BINARY), [(None, m._SNAP_CHROMIUM_BINARY)])

    def test_non_snap_browser_uses_selenium_manager(self):
        self.assertEqual(m._driver_choices("/usr/bin/google-chrome"), [(None, "/usr/bin/google-chrome")])

    def test_falls_through_to_the_next_driver_when_one_will_not_start(self):
        from selenium.common.exceptions import WebDriverException
        tried = []
        def chrome(options, service):
            tried.append(service.path if service else None)
            if len(tried) < 3:
                raise WebDriverException("Service unexpectedly exited. Status code was: 1")
            return "DRIVER"
        pairs = [("/a/driver", "/a/chrome"), ("/b/driver", "/b/chrome"), (None, "/c/chrome")]
        with patch.object(m, "_driver_choices", return_value=pairs), patch.object(m.webdriver, "Chrome", chrome), \
             patch.object(m, "_repair_stale_snap_symlink"), patch.object(m, "_kill_stray_chrome_for_profile"), \
             patch.object(m, "_CHROMIUM_PROFILE_DIR", Path(tempfile.mkdtemp())):
            self.assertEqual(m._setup_driver(), "DRIVER")
        self.assertEqual(tried, ["/a/driver", "/b/driver", None])

    def test_all_failing_raises_the_last_error(self):
        from selenium.common.exceptions import WebDriverException
        def chrome(options, service):
            raise WebDriverException("nope")
        with patch.object(m, "_driver_choices", return_value=[(None, "/x")]), patch.object(m.webdriver, "Chrome", chrome), \
             patch.object(m, "_repair_stale_snap_symlink"), patch.object(m, "_kill_stray_chrome_for_profile"), \
             patch.object(m, "_CHROMIUM_PROFILE_DIR", Path(tempfile.mkdtemp())):
            with self.assertRaises(WebDriverException):
                m._setup_driver()
