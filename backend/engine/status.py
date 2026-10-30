"""`python3 cli.py status` -- one screen answering "is it healthy, and what is it doing?"."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.backup_db import list_backups
from engine.database import TradingDB

IST = ZoneInfo("Asia/Kolkata")


def collect(account: str = "CRYPTO_DIP_REVERSAL", db_path=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    db = TradingDB(db_path)
    acct = db.get_account(account) or {}
    today = now.strftime("%Y-%m-%d")
    todays = db.get_trades_for_date(today, account)
    try:                                       # the same timer/journal check as the Telegram monitor and the website
        from engine import engine_monitor
        engine = engine_monitor.engine_status(account)
    except Exception as e:
        engine = {"ok": None, "text": f"engine status unavailable ({type(e).__name__})"}
    token_hours = None
    try:
        from engine.config import UpstoxConfig
        from services.auth.upstox_auto_login import token_expiry_epoch
        exp = token_expiry_epoch(UpstoxConfig.ACCESS_TOKEN)
        token_hours = (exp - time.time()) / 3600 if exp else None
    except Exception:
        pass
    backups = list_backups()
    return {
        "now": now, "engine": engine,
        "capital": acct.get("current_capital", acct.get("capital")), "initial": acct.get("initial_capital"),
        "day_trades": len(todays), "day_pnl": sum(float(t.get("net_pnl") or 0) for t in todays),
        "open_positions": db.get_open_positions(account), "token_hours": token_hours,
        "last_backup": datetime.fromtimestamp(backups[0].stat().st_mtime, IST) if backups else None,
    }


def render(s: dict) -> str:
    now = s["now"]
    out = [f"STATUS  {now:%Y-%m-%d %H:%M:%S} IST", ""]
    e = s["engine"]
    out.append(f"Engine      {'OK' if e.get('ok') else 'UNKNOWN' if e.get('ok') is None else 'PROBLEM'}   {e.get('text')}")
    if s["capital"] is not None:
        out.append(f"Capital     Rs{s['capital']:,.2f}" + (f"  (started Rs{s['initial']:,.2f}, {s['capital'] - s['initial']:+,.2f})" if s["initial"] else ""))
    out.append(f"Today       {s['day_trades']} trade(s), net Rs{s['day_pnl']:+,.2f}")
    if s["open_positions"]:
        out.append(f"Open        {len(s['open_positions'])}:")
        for p in s["open_positions"]:
            out.append(f"  {p['symbol']:<10s}{p['direction']:<6s} qty {p['qty']:<4} @ {p['entry_price']}  stop {round(p['current_stop'], 2)}  target {round(p['target_price'], 2) if p.get('target_price') else '-'}")
    else:
        out.append("Open        none")
    th = s["token_hours"]
    out.append("Token       " + ("unknown" if th is None else f"expires in {th:.1f} h" if th > 0 else "EXPIRED"))
    lb = s["last_backup"]
    out.append("Last backup " + ("NONE" if lb is None else f"{lb:%Y-%m-%d %H:%M} ({(now - lb).total_seconds() / 3600:.1f} h ago)"))
    return "\n".join(out)


def main(account: str = "CRYPTO_DIP_REVERSAL") -> None:
    print(render(collect(account)))
