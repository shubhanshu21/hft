"""
utils/telegram.py — Fire-and-forget Telegram alerts for the dry-run/live bots.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from backend/.env (via config.py's
dotenv load). Blank/missing config means alerts are silently disabled — bots
must keep running fine without Telegram configured.

Never raises: a Telegram outage or bad config must not crash the trading
loop it's alerting from, so every send failure is logged and swallowed.
"""

from core.paths import BACKEND_ROOT
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=BACKEND_ROOT / ".env")

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage" if _BOT_TOKEN else ""
_PHOTO_API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendPhoto" if _BOT_TOKEN else ""
_UPDATES_API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates" if _BOT_TOKEN else ""

_log = logging.getLogger("telegram")

ENABLED = bool(_BOT_TOKEN and _CHAT_ID)


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns False (and logs) on any failure or if unconfigured."""
    if not ENABLED:
        return False
    try:
        resp = requests.post(
            _API_URL,
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            _log.warning("Telegram send failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        _log.warning("Telegram send raised: %s", exc)
        return False


def send_photo(path, caption: str = "") -> bool:
    """Send a local image file (e.g. the equity curve PNG). Returns False (and logs) on any failure or if unconfigured."""
    if not ENABLED:
        return False
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                _PHOTO_API_URL,
                data={"chat_id": _CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=30,
            )
        if resp.status_code != 200:
            _log.warning("Telegram send_photo failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        _log.warning("Telegram send_photo raised: %s", exc)
        return False


def get_updates(offset: int = None, timeout: int = 0) -> list:
    """Poll for inbound messages (used for the start/stop kill switch).

    Returns every update seen -- including ones from chats other than
    TELEGRAM_CHAT_ID -- so the caller can advance its offset past them
    without replaying them forever; is_authorized_chat() is what actually
    gates whether a message's *command* gets acted on. Returns [] on any
    failure, timeout, or if unconfigured (never raises, same contract as
    send()/send_photo()).
    """
    if not ENABLED:
        return []
    try:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(_UPDATES_API_URL, params=params, timeout=timeout + 10)
        if resp.status_code != 200:
            _log.warning("Telegram get_updates failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return []
        out = []
        for u in resp.json().get("result", []):
            msg = u.get("message") or {}
            out.append({
                "update_id": u["update_id"],
                "chat_id": str(msg.get("chat", {}).get("id", "")),
                "text": (msg.get("text") or "").strip(),
            })
        return out
    except Exception as exc:
        _log.warning("Telegram get_updates raised: %s", exc)
        return []


def is_authorized_chat(chat_id: str) -> bool:
    """Only the configured TELEGRAM_CHAT_ID may issue trading commands --
    this is a single-operator control channel, not a public bot."""
    return ENABLED and str(chat_id) == str(_CHAT_ID)


def alert_error(context: str, exc: Exception) -> None:
    send(f"🔴 <b>ERROR</b> — {context}\n<code>{type(exc).__name__}: {exc}</code>")


def alert_entry(sig: dict, capital: float) -> None:
    arrow = "🟢 LONG" if sig["direction"] == "long" else "🔴 SHORT"
    stype = sig.get('setup_type', 'trend_breakout').replace('_', ' ').title()
    tp_val = sig.get('tp')
    tp_str = f"₹{tp_val:.2f}" if tp_val is not None else "Dynamic Trail"
    # Leverage shown directly (trade_value / margin_used) instead of a raw
    # margin rupee figure next to total balance -- those two numbers being
    # side by side read as if margin were subtracted from balance, which it
    # isn't (balance only moves on close). Leverage is the number that
    # actually answers "how geared is this trade."
    leverage = sig.get("leverage")
    if leverage is None and sig.get("margin_used"):
        leverage = sig["trade_value"] / sig["margin_used"]
    lev_line = f"\nLeverage: {leverage:.1f}x  (Margin: ₹{sig['margin_used']:,.2f})" if leverage else ""
    send(
        f"📥 <b>ENTRY</b> {sig['symbol']} {arrow}\n"
        f"Strategy: <b>{stype}</b>\n"
        f"Price: ₹{sig['entry_price']:.2f}  Qty: {sig['qty']}\n"
        f"SL: ₹{sig['sl']:.2f}  TP: {tp_str}{lev_line}\n"
        f"Balance: ₹{capital:,.2f}"
    )


def alert_exit(sym: str, pos: dict, exit_p: float, reason: str, net_pnl: float, capital: float) -> None:
    emoji = "✅" if net_pnl >= 0 else "❌"
    send(
        f"{emoji} <b>EXIT</b> {sym} {pos['direction'].upper()} @ ₹{exit_p:.2f}  [{reason}]\n"
        f"Net PnL: ₹{net_pnl:+,.2f}\n"
        f"Balance (realized equity): ₹{capital:,.2f}"
    )
