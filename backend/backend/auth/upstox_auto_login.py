"""
auth/upstox_auto_login.py — headless daily Upstox login via Selenium, so
you don't have to run auth.upstox_auth's manual flow every morning.

Adapted from a Selenium-driven login flow originally written against
Upstox's real login page, wired into this project's own OAuth machinery
(auth.upstox_auth.UpstoxAuthClient) instead of duplicating the code
exchange. Deliberately minimal compared to where this was adapted from:
no database, no Telegram/notification integration, no cross-process
broker-cache invalidation (those all assumed a specific multi-process app
this project isn't). The refreshed token is written to
cache/upstox_token.json (not .env — see config.py's module docstring for
why) via UpstoxConfig.save_access_token() — call
ensure_fresh_upstox_token()'s `on_token_refreshed` callback if you have a
long-lived UpstoxBroker instance that also needs the new token
(UpstoxBroker bakes its token in at construction time and won't pick up
a cache-file change on its own; see UpstoxBroker.set_access_token()).

SECURITY WARNING — read before enabling:
Unlike the access token itself (daily-expiring, revocable, API-scoped), the
UPSTOX_TOTP_SECRET this needs is the actual seed your 2FA app uses to
generate codes. Anyone who gets it (plus UPSTOX_PIN) has permanent, full
login access to the real account — 2FA no longer protects it once both
are on disk. This also drives Upstox's real login page via Selenium/XPath
selectors (not just the official OAuth token-exchange call), so it WILL
break silently if Upstox changes their login page's markup, and this
kind of UI automation may fall outside their normal API ToS.

Auto-login is entirely opt-in: it only runs if UPSTOX_USERNAME/PIN/
TOTP_SECRET are ALL set (see UpstoxConfig.auto_login_configured). Leave
them blank to keep using the manual `python3 -m auth.upstox_auth` flow
(~30s/day) instead.

Usage:
    python3 -m auth.upstox_auto_login       # force a refresh right now
    ensure_fresh_upstox_token()             # importable, no-ops if not
                                             # configured, checks validity
                                             # before re-logging-in
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Callable

import pyotp
import upstox_client
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from upstox_client.rest import ApiException

from auth.upstox_auth import UpstoxAuthClient
from config import UpstoxConfig
from utils.logger import get_logger

log = get_logger(__name__)

_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"


def token_expiry_epoch(token: str) -> float | None:
    """
    Read the `exp` claim (unix seconds) straight out of the access token's
    JWT payload, without verifying the signature — this only ever reads a
    token this app already trusts (issued by Upstox, read back from our
    own cache/upstox_token.json), never one from an untrusted source, so there's nothing to
    verify against. Returns None if `token` is empty or isn't a parseable
    JWT — callers should fall back to their own periodic-check interval
    in that case, not treat it as "never expires".
    """
    if not token:
        return None
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return float(claims["exp"])
    except Exception:
        return None


# Always-liquid, always-listed instrument used purely as a validation
# probe — NOT a real quote request. Works regardless of market hours.
_VALIDATION_INSTRUMENT = "NSE_INDEX|Nifty 50"


def _token_is_valid(token: str) -> bool:
    """Real check against Upstox's own market-quote v3 LTP endpoint — the same one UpstoxBroker.get_ltp/get_ltp_batch actually depend on."""
    if not token:
        return False
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        response = upstox_client.MarketQuoteV3Api(upstox_client.ApiClient(configuration)).get_ltp(instrument_key=_VALIDATION_INSTRUMENT)
        return bool(response.data)
    except ApiException as exc:
        if exc.status == 401:
            log.info("Existing Upstox token is invalid/expired.")
        else:
            log.warning("Upstox token validation error (treating as invalid): HTTP %s — %s", exc.status, exc.reason)
        return False
    except Exception as exc:
        log.warning("Upstox token validation failed unexpectedly (treating as invalid): %s", exc)
        return False


def token_status() -> dict:
    """{"configured": bool, "valid": bool, "expiry_epoch": float | None} for the current UPSTOX_ACCESS_TOKEN. `valid` is a real live LTP-endpoint probe, not a locally-guessed one."""
    token = UpstoxConfig.ACCESS_TOKEN
    return {
        "configured": UpstoxConfig.auto_login_configured(),
        "valid": _token_is_valid(token),
        "expiry_epoch": token_expiry_epoch(token) if token else None,
    }


_SNAP_CHROMIUM_BINARY = "/snap/bin/chromium"
_SNAP_CHROMIUM_DRIVER = "/snap/chromium/current/usr/lib/chromium-browser/chromedriver"


def _find_chromium_binary() -> str | None:
    for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", _SNAP_CHROMIUM_BINARY):
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    return None


def _find_chromium_driver(binary: str | None) -> str | None:
    """
    For the snap-packaged Chromium specifically, use the EXACT chromedriver
    bundled inside that same snap revision, not whatever Selenium Manager
    would otherwise auto-download.

    Verified directly that these CAN drift apart: Selenium Manager
    resolved chromedriver 151.0.7922.138 while the installed snap
    Chromium was 151.0.7922.108 (a different patch build) — a real gap,
    worth pinning shut on general principle, though it turned out NOT to
    be the cause of a since-diagnosed ~2min stall during login (that was
    ordinary network latency to Upstox's servers from this host, not a
    driver/browser issue — see auth.upstox_auto_login's module docstring
    and rsi_monitor._AUTO_LOGIN_TIMEOUT_SEC). For any OTHER browser (a
    real Chrome/Chromium install, not the snap), Selenium Manager's own
    version-matching is trustworthy as normal — only the snap needs this
    override, since the pip-installed `selenium` package can't see which
    exact chromedriver snapd bundled alongside it.
    """
    if binary == _SNAP_CHROMIUM_BINARY and Path(_SNAP_CHROMIUM_DRIVER).exists():
        return _SNAP_CHROMIUM_DRIVER
    return None


def _profile_dir_for(binary: str | None) -> Path:
    """
    Snap's AppArmor confinement only allows Chromium to write under its
    own ~/snap/chromium/ data dir — a --user-data-dir anywhere else in
    $HOME (or /tmp) risks "Failed to create SingletonLock: Permission
    denied" for the snap-packaged browser specifically. Any other browser
    (a real .deb/rpm Chrome/Chromium install) has no such confinement and
    can use a plain cache directory.
    """
    if binary == _SNAP_CHROMIUM_BINARY:
        return Path.home() / "snap" / "chromium" / "current" / "selenium-profile"
    return Path.home() / ".cache" / "options-upstox-login"


_CHROMIUM_BINARY = _find_chromium_binary()
_CHROMIUM_DRIVER = _find_chromium_driver(_CHROMIUM_BINARY)
_CHROMIUM_PROFILE_DIR = _profile_dir_for(_CHROMIUM_BINARY)


def _kill_stray_chrome_for_profile() -> None:
    """
    Chrome refuses to start a second instance against a --user-data-dir
    that's already locked (SingletonLock) — a prior login attempt whose
    process got killed (container restart, Ctrl+C mid-flight) can leave
    that lock held forever. Safe to unconditionally clear here: this is
    called immediately before creating our OWN new driver, so anything
    still touching this profile directory is by definition a stray from
    an earlier, no-longer-relevant attempt, not a session we still need.
    """
    import signal

    profile = str(_CHROMIUM_PROFILE_DIR)
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if profile in cmdline and "chrom" in cmdline.lower():
            try:
                os.kill(int(pid_dir.name), signal.SIGKILL)
                log.warning("Killed stray Chrome/chromedriver process (pid=%s) from a previous login attempt.", pid_dir.name)
            except OSError:
                pass
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (_CHROMIUM_PROFILE_DIR / name).unlink(missing_ok=True)


def _setup_driver() -> webdriver.Chrome:
    _CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _kill_stray_chrome_for_profile()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"--user-data-dir={_CHROMIUM_PROFILE_DIR}")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")

    if _CHROMIUM_BINARY:
        options.binary_location = _CHROMIUM_BINARY

    # For the snap Chromium, pin the exact chromedriver bundled in that
    # same snap revision instead of Selenium Manager's own resolution —
    # see _find_chromium_driver's docstring for the mismatch this avoids.
    # Any other (real, non-snap) browser install still uses Selenium
    # Manager's normal auto-resolution, which is trustworthy there.
    from selenium.webdriver.chrome.service import Service
    service = Service(_CHROMIUM_DRIVER) if _CHROMIUM_DRIVER else None
    return webdriver.Chrome(options=options, service=service)


def _js_click(driver, element) -> None:
    """Real click() on these buttons intermittently hits Upstox's own 'OTP sent' toast overlay — a JS click bypasses that instead of guessing a fixed sleep."""
    driver.execute_script("arguments[0].click();", element)


def _auto_login_get_code() -> str:
    """
    Drive Upstox's real login page headlessly; return the OAuth 'code'.
    Element IDs below (mobileNum/getOtp/otpNum/continueBtn/pinCode/
    pinContinueBtn) come from Upstox's live login page markup, not
    generic tag/type selectors (their OTP/TOTP field has no `maxlength`
    attribute; their buttons are `id`-only, not text-matchable).

    Upstox shows one of two screens depending on whether it recognizes
    this browser: the full mobile-number -> OTP/TOTP -> PIN flow for a
    fresh session, or a "Welcome back, enter PIN" shortcut straight to
    `pinCode` once this browser profile already has a remembered session
    (see _CHROMIUM_PROFILE_DIR — reused across attempts for exactly this
    reason). Waits for whichever field shows up first and branches
    accordingly, rather than assuming only the full flow.
    """
    driver = _setup_driver()
    try:
        params = {
            "response_type": "code",
            "client_id": UpstoxConfig.API_KEY,
            "redirect_uri": UpstoxConfig.REDIRECT_URI,
        }
        driver.get(f"{_LOGIN_URL}?{urllib.parse.urlencode(params)}")

        first_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#mobileNum, #pinCode"))
        )

        if first_field.get_attribute("id") == "mobileNum":
            first_field.clear()
            first_field.send_keys(UpstoxConfig.USERNAME)
            driver.find_element(By.ID, "getOtp").click()
            log.info("Upstox auto-login: mobile number submitted.")

            totp_code = pyotp.TOTP(UpstoxConfig.TOTP_SECRET).now()
            otp_field = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "otpNum")))
            otp_field.clear()
            otp_field.send_keys(totp_code)
            continue_btn = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "continueBtn")))
            WebDriverWait(driver, 10).until(lambda d: continue_btn.get_attribute("disabled") is None)
            _js_click(driver, continue_btn)
            log.info("Upstox auto-login: TOTP submitted.")

            pin_field = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "pinCode")))
        else:
            log.info("Upstox auto-login: browser already recognized — skipping straight to PIN.")
            pin_field = first_field

        pin_field.clear()
        pin_field.send_keys(UpstoxConfig.PIN)
        pin_continue_btn = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "pinContinueBtn")))
        WebDriverWait(driver, 10).until(lambda d: pin_continue_btn.get_attribute("disabled") is None)
        _js_click(driver, pin_continue_btn)
        log.info("Upstox auto-login: PIN submitted, waiting for redirect ...")

        WebDriverWait(driver, 30).until(lambda d: "code=" in d.current_url)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(driver.current_url).query)["code"][0]
        log.info("Upstox auto-login: authorization code received.")
        return code
    finally:
        driver.quit()


def ensure_fresh_upstox_token(force: bool = False, on_token_refreshed: Callable[[str], None] | None = None) -> str | None:
    """
    Ensure UpstoxConfig.ACCESS_TOKEN is valid, refreshing it headlessly if
    needed and persisting the result to cache/upstox_token.json. No-ops (returns None
    immediately, no Selenium/network call) if auto-login isn't configured
    — see UpstoxConfig.auto_login_configured(); treat that as "fall back
    to the manual daily refresh," not a failure.

    on_token_refreshed, if given, is called with the new token after a
    successful refresh — use it to push the token into any already-
    constructed UpstoxBroker instance via its set_access_token() (this
    module has no registry of running brokers to update on its own).

    Returns the (possibly unchanged) valid token, or None if a refresh
    was needed but failed.
    """
    if not UpstoxConfig.auto_login_configured():
        return None

    if not force and _token_is_valid(UpstoxConfig.ACCESS_TOKEN):
        log.info("Existing Upstox token still valid — no refresh needed.")
        return UpstoxConfig.ACCESS_TOKEN

    log.info("Refreshing Upstox token via headless auto-login ...")
    try:
        code = _auto_login_get_code()
        auth = UpstoxAuthClient(api_key=UpstoxConfig.API_KEY, api_secret=UpstoxConfig.API_SECRET, redirect_uri=UpstoxConfig.REDIRECT_URI)
        token = auth.exchange_code_for_token(code)
        UpstoxConfig.save_access_token(token)
        if on_token_refreshed is not None:
            on_token_refreshed(token)
        log.info("Upstox token refreshed automatically.")
        return token
    except Exception as exc:
        log.critical(
            "Automatic Upstox login failed: %s. Falling back — run `python3 -m auth.upstox_auth` "
            "manually before market open or entries will fail.", exc, exc_info=True,
        )
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    result = ensure_fresh_upstox_token(force=True)
    sys.exit(0 if result else 1)
