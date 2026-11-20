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


def inr(v: float) -> str:
    """Indian digit grouping without the sign: 100000 -> '1,00,000'."""
    n = str(int(round(abs(v))))
    head, tail = n[:-3], n[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return (head + "," + tail) if head else tail


# ---------------------------------------------------------------------------------------------------------------------------
# One simple message format for every alert (2026-11-18, user: "make telegram alerts simple so that i can properly check them").
#   🟢 BOUGHT · <account>            ✅ / ❌ SOLD · <account>          📊 <account> · <what>          ⚠️ PROBLEM · <what>
# Each trade is a short block: the stock and size, the money, why, and the stop; every message ends with the account value.
# ---------------------------------------------------------------------------------------------------------------------------
WHY = {   # stored exit reasons -> plain words
    "stop": "hit the stop-loss", "end_of_day": "day trade, sold at 15:20", "missed_exit": "sold at the next run (the 15:20 sale was missed)",
    "held_40_days": "held 40 trading days, as planned", "dead_money": "still below the buy price after 20 trading days",
    "rebalance": "no longer in the top 3", "rebalance_next_contract": "moved to the next month's contract",
    "roll_before_expiry": "contract about to expire, moved to the next month", "trimmed": "position made smaller at the re-check",
    "momentum_out": "left the top 20 by 12-month momentum (monthly check)",
    "strategy_closed": "strategy closed (one equity account), sold at the open",
}


def price(v: float) -> str:
    """Prices with enough digits: DOGE 0.06819, AVAX 7.942, SBIN 812.40."""
    v = float(v)
    return f"{v:,.5g}" if abs(v) < 1 else f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.2f}"


_p = price      # the blocks' parameters are called `price`, so they use this alias


def rupees(v: float) -> str:
    """₹1,23,456 (no sign)."""
    return f"₹{inr(v)}"


def signed_rupees(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}₹{inr(v)}"


def why_text(reason: str) -> str:
    r = str(reason or "")
    low = r.lower()
    if low.startswith("take profit"):
        return f"hit the {r[r.find('(') + 1:r.find(')')]} profit target" if "(" in r else "hit the profit target"
    if low.startswith("stop loss"):
        return "hit the stop-loss"
    if low.startswith("exit signal"):
        return "price back up to its normal level"
    if low.startswith("time stop"):
        return "24 hours passed"
    return WHY.get(r, r.replace("_", " "))


def bought_block(sym: str, size: str, price: float, money: float, why: str = "", stop: float | None = None, risk: float | None = None) -> str:
    lines = [f"<b>{sym}</b> · {size} at {_p(price)} = {rupees(money)}"]
    if why:
        lines.append(f"Why: {why}")
    if stop:
        lines.append(f"Stop: {_p(stop)}" + (f" (most it can lose ≈ {rupees(risk)})" if risk else ""))
    return "\n".join(lines)


def sold_block(sym: str, size: str, buy: float, sell: float, profit: float, fees: float | None = None, why: str = "", held: str = "") -> str:
    lines = [f"<b>{sym}</b> · {size} · {_p(buy)} → {_p(sell)}",
             f"{'Profit' if profit >= 0 else 'Loss'}: <b>{signed_rupees(profit)}</b>" + (f" after {rupees(fees)} fees" if fees is not None else "")]
    if why or held:
        lines.append("Why: " + " · ".join(x for x in (why, f"held {held}" if held else "") if x))
    return "\n".join(lines)


def account_line(value: float, start: float | None = None) -> str:
    if not start:
        return f"Account: <b>{rupees(value)}</b>"
    pct = f"{(value / start - 1) * 100:+.1f}%".replace("-", "−")          # the same minus sign as the rupee amounts
    return f"Account: <b>{rupees(value)}</b> ({pct} since start)"


def message(icon: str, title: str, blocks: list[str], value: float | None = None, start: float | None = None) -> str:
    """'<icon> <title>', the blocks one blank line apart, then the account value."""
    parts = [f"{icon} <b>{title}</b>", *[b for b in blocks if b]]
    if value is not None:
        parts.append(account_line(value, start))
    return "\n\n".join(parts)


def trade_icon(sold_profits: list[float], bought: int) -> str:
    """🟢 only buys · ✅ sold at a profit · ❌ sold at a loss · 📊 nothing traded."""
    if sold_profits:
        return "✅" if sum(sold_profits) >= 0 else "❌"
    return "🟢" if bought else "📊"


def problem(what: str, detail: str, check: str = "") -> str:
    return message("⚠️", f"PROBLEM · {what}", [detail, f"Check: <code>{check}</code>" if check else ""])
