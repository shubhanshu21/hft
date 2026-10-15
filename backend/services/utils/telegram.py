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


def inr(v: float) -> str:
    """Indian digit grouping without the sign: 100000 -> '1,00,000'."""
    n = str(int(round(abs(v))))
    head, tail = n[:-3], n[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return (head + "," + tail) if head else tail


def _rs(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}₹{inr(v)}"


def _size(symbol: str, qty: int, lot: int) -> str:
    """'2 lots = 2,000 EUR' for currency futures, '35 shares' for equity."""
    if lot and lot > 1:
        return f"{qty} lot{'s' if qty != 1 else ''} = {inr(qty * lot)} {symbol[:3]}"
    return f"{qty} share{'s' if qty != 1 else ''}"


def alert_entry(sig: dict, capital: float, free: float | None = None) -> None:
    """A new paper trade: what was bought, how much money is at stake, where the stop is and what it would lose there."""
    long_ = sig["direction"] == "long"
    qty = sig.get("lots", sig["qty"])
    lot = sig.get("lot_size") or (round(sig["trade_value"] / (qty * sig["entry_price"])) if qty and sig["entry_price"] else 1)
    risk = abs(sig["entry_price"] - sig["sl"]) * qty * lot
    tp = sig.get("tp")
    margin = sig.get("margin_used")
    if lot > 1 and margin:
        money = f"Margin blocked: ₹{inr(margin)} ({sig['trade_value'] / margin:.0f}x of ₹{inr(sig['trade_value'])})"
    else:
        money = f"Money used: ₹{inr(sig['trade_value'])}"
    send(
        f"📥 <b>{'BUY' if long_ else 'SELL'} {sig['symbol']}</b> ({sig.get('strategy', '')})\n"
        f"{_size(sig['symbol'], qty, lot)} @ ₹{sig['entry_price']:,.2f}\n"
        f"Stop: ₹{sig['sl']:,.2f} (loss there ≈ ₹{inr(risk)}" + (f", {sig['risk_pct']:g}% risk)" if sig.get("risk_pct") else ")") + "\n"
        f"Target: " + (f"₹{tp:,.2f}" if tp else "none, the strategy's exit rule decides") + "\n"
        f"{money}\n"
        f"Account: ₹{inr(capital)}" + (f" · free after this: ₹{inr(free)}" if free is not None else "")
    )


def alert_exit(sym: str, pos: dict, exit_p: float, reason: str, net_pnl: float, capital: float, gross: float | None = None,
               fees: float | None = None, now=None, lot_size: int | None = None) -> None:
    """A closed paper trade: buy and sell price, how long it was held, profit before and after fees, and the account after it."""
    qty = pos.get("lots", pos.get("qty", 1))
    lot = lot_size or pos.get("lot_size") or 1
    held = ""
    t0 = pos.get("entry_time")
    if now is not None and hasattr(t0, "strftime"):
        try:
            d = now - t0
            held = f" · held {d.days} day{'s' if d.days != 1 else ''}" if d.days >= 1 else f" · held {d.seconds // 3600}h {d.seconds % 3600 // 60}m"
        except TypeError:
            pass
    money = f"Before fees {_rs(gross)} · fees ₹{inr(fees)} · " if gross is not None and fees is not None else ""
    send(
        f"{'✅' if net_pnl >= 0 else '❌'} <b>{'SOLD' if pos['direction'] == 'long' else 'BOUGHT BACK'} {sym}</b> ({pos.get('strategy', '')}) — "
        f"{str(reason).replace('_', ' ')}\n"
        f"{_size(sym, qty, lot)} @ ₹{pos['entry_price']:,.2f} → ₹{exit_p:,.2f}{held}\n"
        f"{money}<b>profit {_rs(net_pnl)}</b>\n"
        f"Account now: ₹{inr(capital)}"
    )
