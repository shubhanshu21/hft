"""Shared backtest pieces: BacktestError (a request that cannot run; its message is shown on the website) and metrics() (the summary
numbers every strategy's own backtest reports). The generic daily-bar runner that used to live here was removed 2026-11-10 (unused)."""
from __future__ import annotations

import math


class BacktestError(ValueError):
    """The request cannot be run (bad dates, no data, unsupported strategy). The message is shown to the user."""


def metrics(equity: list[int], capital: float, closed: list[dict], n_days: int, still_open: int) -> dict:
    final = equity[-1]
    wins = [c for c in closed if c["net"] > 0]
    gross_win = sum(c["net"] for c in wins)
    gross_loss = -sum(c["net"] for c in closed if c["net"] <= 0)
    peak, dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    years = max(n_days / 250, 1e-9)
    return {
        "start_capital": round(capital), "final_equity": final, "return_pct": round((final / capital - 1) * 100, 2),
        "cagr_pct": round(((final / capital) ** (1 / years) - 1) * 100, 2) if final > 0 else -100.0,
        "max_drawdown_pct": round(dd * 100, 2), "trades": len(closed), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not closed else math.inf if gross_win else 0.0),
        "closed_profit": round(sum(c["net"] for c in closed)), "open_profit": round(final - capital - sum(c["net"] for c in closed)),
        "gross_profit": round(sum(c["gross"] for c in closed)), "fees": round(sum(c["fees"] for c in closed)),
        "slippage": round(sum(c["slip"] for c in closed)), "avg_hold_days": round(sum(c["days"] for c in closed) / len(closed), 1) if closed else 0.0,
        "open_at_end": still_open, "trading_days": n_days,
    }
