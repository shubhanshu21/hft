"""
config.py — Upstox credentials, loaded from backend/.env.

Deliberately minimal compared to automate's config.py: no database-backed
token store, no scheduler, no Telegram alerting. USERNAME/PIN/TOTP_SECRET
are only needed for auth.upstox_auto_login's headless Selenium login —
leave them blank to use auth.upstox_auth's manual flow instead (see
auto_login_configured()).

ACCESS_TOKEN is deliberately NOT read from/written to .env like the other
credentials: it's a value auth.upstox_auth/auth.upstox_auto_login rewrite
constantly (daily at minimum -- observed rewriting multiple times within
minutes during a debugging session, since every fresh login invalidates
whatever token existed before it), whereas .env is meant to hold static
app credentials someone might reasonably want under source control review
or backed up separately. It's cached instead at cache/upstox_token.json,
the same daily-cache directory holding the instrument master and index
constituent lists. If you refresh ACCESS_TOKEN yourself,
UpstoxConfig.save_access_token() writes it there for the next process
start; an already-running UpstoxBroker needs UpstoxBroker.set_access_token()
called on it directly since it bakes the token into the SDK client at
construction time.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)

_TOKEN_CACHE_PATH = Path(__file__).resolve().parent / "cache" / "upstox_token.json"


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _load_access_token() -> str:
    """Prefers the cached token from cache/upstox_token.json; falls back to UPSTOX_ACCESS_TOKEN in .env only if that cache file doesn't exist yet (e.g. a token pasted manually before ever running the auth flow)."""
    try:
        token = json.loads(_TOKEN_CACHE_PATH.read_text()).get("access_token", "")
        if token:
            return token
    except (OSError, json.JSONDecodeError):
        pass
    return _optional("UPSTOX_ACCESS_TOKEN")


class UpstoxConfig:
    """Upstox OAuth2 credentials."""

    API_KEY: str = _optional("UPSTOX_API_KEY")
    API_SECRET: str = _optional("UPSTOX_API_SECRET")
    REDIRECT_URI: str = _optional("UPSTOX_REDIRECT_URI", "https://127.0.0.1/")
    ACCESS_TOKEN: str = _load_access_token()

    # Optional — only needed for auth/upstox_auto_login.py's headless daily
    # token refresh. SECURITY: unlike ACCESS_TOKEN (daily-expiring,
    # revocable, API-scoped), TOTP_SECRET is the seed your 2FA app uses to
    # generate codes — anyone with it + PIN has permanent full login access
    # to the real account. Leave blank to keep using the manual
    # `python3 -m auth.upstox_auth` flow instead.
    USERNAME: str = _optional("UPSTOX_USERNAME")  # mobile number
    PIN: str = _optional("UPSTOX_PIN")
    TOTP_SECRET: str = _optional("UPSTOX_TOTP_SECRET")

    # Capital & Leverage configuration (used for live/paper execution and backtests)
    # Default leverage is 1.0 (conservative, 100% cash / no leverage). Can be set to e.g. 4.0 for MIS margin.
    TRADING_CAPITAL: float = float(_optional("TRADING_CAPITAL", "100000.0"))
    INTRADAY_LEVERAGE: float = float(_optional("INTRADAY_LEVERAGE", "2.0"))

    # Trade Direction Control: "both", "long-only", or "short-only"
    TRADING_DIRECTION: str = _optional("TRADING_DIRECTION", "both").lower()
    ALLOW_SHORTS: bool = _optional("ALLOW_SHORTS", "true").lower() in ("true", "1", "yes") and TRADING_DIRECTION != "long-only"

    @classmethod
    def validate(cls) -> None:
        """Raise SystemExit if the OAuth app credentials are incomplete (ACCESS_TOKEN is expected to be blank/stale until you log in)."""
        for attr, key in [("API_KEY", "UPSTOX_API_KEY"), ("API_SECRET", "UPSTOX_API_SECRET")]:
            if not getattr(cls, attr):
                logging.getLogger("config").critical("Missing required env var '%s' for Upstox.", key)
                sys.exit(1)

    @classmethod
    def auto_login_configured(cls) -> bool:
        return bool(cls.USERNAME and cls.PIN and cls.TOTP_SECRET)

    @classmethod
    def save_access_token(cls, token: str) -> None:
        """Persist a freshly-obtained access_token to cache/upstox_token.json (not .env — see module docstring) and update this process's in-memory copy."""
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_PATH.write_text(json.dumps({
            "access_token": token,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }))
        cls.ACCESS_TOKEN = token
