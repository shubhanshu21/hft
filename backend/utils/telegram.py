"""
utils/telegram.py — Fire-and-forget Telegram alerts for the dry-run/live bots.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from backend/.env (via config.py's
dotenv load). Blank/missing config means alerts are silently disabled — bots
must keep running fine without Telegram configured.

Never raises: a Telegram outage or bad config must not crash the trading
loop it's alerting from, so every send failure is logged and swallowed.
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage" if _BOT_TOKEN else ""

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


def alert_error(context: str, exc: Exception) -> None:
    send(f"🔴 <b>ERROR</b> — {context}\n<code>{type(exc).__name__}: {exc}</code>")


def alert_entry(sig: dict) -> None:
    arrow = "🟢 LONG" if sig["direction"] == "long" else "🔴 SHORT"
    send(
        f"📥 <b>ENTRY</b> {sig['symbol']} {arrow}\n"
        f"Price: ₹{sig['entry_price']:.2f}  Qty: {sig['qty']}\n"
        f"SL: ₹{sig['sl']:.2f}  TP: ₹{sig['tp']:.2f}"
    )


def alert_exit(sym: str, pos: dict, exit_p: float, reason: str, net_pnl: float, capital: float) -> None:
    emoji = "✅" if net_pnl >= 0 else "❌"
    send(
        f"{emoji} <b>EXIT</b> {sym} {pos['direction'].upper()} @ ₹{exit_p:.2f}  [{reason}]\n"
        f"Net PnL: ₹{net_pnl:+,.2f}\n"
        f"Balance: ₹{capital:,.2f}"
    )
