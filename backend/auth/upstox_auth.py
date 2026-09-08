"""
auth/upstox_auth.py — Upstox OAuth2 Authorization Code flow.

A fresh access_token must be generated each trading day. This module
provides:

  1. A helper to build the authorization URL.
  2. A helper to exchange the authorization code for an access_token.
  3. A CLI mode to run the flow interactively.

Typical daily workflow:
  $ python3 -m auth.upstox_auth
  -> Prints a login URL
  -> Open it in a browser, log in, and copy the `code` from the redirect URL
  -> Paste the code (or the full redirect URL) back into the terminal
  -> The script prints the new access_token and optionally saves it to
     cache/upstox_token.json

Security notes:
  - client_secret is read from environment variables; never hardcoded.
  - The access_token is never logged in plaintext.
  - No auto-login/headless flow here (that needs Selenium + storing a TOTP
    seed, which is a materially bigger security tradeoff) — this is the
    manual, ~30s/day flow only.
"""
import sys
import urllib.parse

import requests

from config import UpstoxConfig
from utils.logger import get_logger

log = get_logger(__name__)

_AUTH_BASE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


class UpstoxAuthClient:
    """
    Handles the Upstox OAuth2 Authorization Code flow:
      Step 1 -> Build the authorization URL and have the user log in.
      Step 2 -> Exchange the returned code for an access_token.
    """

    def __init__(self, api_key: str, api_secret: str, redirect_uri: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(self) -> str:
        """The user opens this URL, logs in (incl. OTP/TOTP), and is redirected to redirect_uri with a `code` query param."""
        params = {
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
        }
        url = f"{_AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"
        log.info("Authorization URL generated (client_id=...%s)", self.api_key[-6:] if self.api_key else "")
        return url

    def exchange_code_for_token(self, authorization_code: str) -> str:
        """Exchange the short-lived authorization code for an access_token. Raises RuntimeError on failure (bad code, wrong credentials, network error)."""
        payload = {
            "code": authorization_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        headers = {"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}

        try:
            log.info("Requesting access_token from Upstox token endpoint...")
            response = requests.post(_TOKEN_URL, data=payload, headers=headers, timeout=15)
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("Token exchange timed out. Check network connectivity.") from exc
        except requests.exceptions.HTTPError as exc:
            # Never log the payload itself — it contains client_secret.
            raise RuntimeError(f"Token exchange failed (HTTP {response.status_code}): {response.text[:200]}") from exc

        data = response.json()
        if "access_token" not in data:
            raise RuntimeError(f"Token exchange returned no access_token. Response: {data}")

        log.info("Access token obtained successfully.")
        return data["access_token"]


def extract_code_from_redirect_url(url: str) -> str:
    """Parse the `code` query parameter out of a pasted redirect URL (e.g. 'https://127.0.0.1/?code=AUTH_CODE&...')."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    codes = params.get("code")
    if not codes:
        raise ValueError("No 'code' parameter found in URL. Make sure you copied the full redirect URL.")
    return codes[0]


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    print("\n" + "=" * 60)
    print("  Upstox Daily Token Generator")
    print("=" * 60)

    UpstoxConfig.validate()
    auth = UpstoxAuthClient(
        api_key=UpstoxConfig.API_KEY,
        api_secret=UpstoxConfig.API_SECRET,
        redirect_uri=UpstoxConfig.REDIRECT_URI,
    )

    login_url = auth.get_authorization_url()
    print("\nStep 1: Open the following URL in your browser and log in:\n")
    print(f"  {login_url}\n")

    print("Step 2: After logging in, paste the FULL redirect URL (or just the 'code') below:")
    user_input = input("  > ").strip()

    if user_input.startswith("http"):
        try:
            code = extract_code_from_redirect_url(user_input)
        except ValueError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    else:
        code = user_input

    try:
        token = auth.exchange_code_for_token(code)
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    print("\n[SUCCESS] New access_token obtained.")

    save = input("\nSave to cache/upstox_token.json now? [y/N]: ").strip().lower()
    if save in ("y", "yes"):
        UpstoxConfig.save_access_token(token)
        print("[SAVED] Token written to cache/upstox_token.json.")
    else:
        print(f"\nToken (not saved):\n{token}\n")
