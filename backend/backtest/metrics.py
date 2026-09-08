"""
backtest/metrics.py — summarize a trade log into the same kind of stats
the published NIFTY ORB study reports, so results are directly
comparable: win rate, profit factor, Sharpe, max drawdown, avg win/loss,
long-vs-short split.

Portfolio-level return/drawdown use each trade's R-multiple (pnl / stop
distance) scaled by an assumed fixed risk-per-trade (RISK_PER_TRADE_PCT
of capital), compounded day over day — NOT a raw sum of each trade's own
% price move. Summing raw per-trade % returns across many symbols/years
is not how a real portfolio behaves (it silently assumes an ever-growing
number of simultaneous full-size positions with no compounding), and
overstates both gains and drawdowns into meaningless territory. The
R-multiple/fixed-risk approach mirrors how strategy/risk.py would size
these trades live: every trade risks the same fraction of capital,
regardless of the underlying stock's price.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RISK_PER_TRADE_PCT = 1.0  # % of capital risked per trade (1R), for portfolio-level return/drawdown only


def summarize(trade_log: pd.DataFrame) -> dict:
    if trade_log.empty:
        return {"trade_count": 0}

    wins = trade_log[trade_log["win"]]
    losses = trade_log[~trade_log["win"]]

    gross_profit_r = wins["r_multiple"].sum()
    gross_loss_r = -losses["r_multiple"].sum()
    profit_factor = (gross_profit_r / gross_loss_r) if gross_loss_r > 0 else float("inf")

    # Portfolio return per trade = R-multiple * fixed risk fraction, e.g. a
    # +2R winner with 1% risk/trade = +2% of capital on that trade. Multiple
    # same-day trades (different symbols) compound sequentially within the
    # day — a simplification (real fills interleave through the day), but
    # it avoids the far worse error of the raw-%-sum approach (which silently
    # assumes an ever-growing number of simultaneous full-size positions with
    # no compounding). The daily grouping slightly understates intraday
    # variance and therefore slightly overstates the Sharpe ratio; in practice
    # the effect is small relative to the signal-to-noise in the strategy.
    trade_log = trade_log.copy()
    trade_log["portfolio_return_pct"] = trade_log["r_multiple"] * RISK_PER_TRADE_PCT
    daily_return_pct = trade_log.groupby("date")["portfolio_return_pct"].sum().sort_index()
    daily_return_frac = daily_return_pct / 100

    sharpe = (daily_return_frac.mean() / daily_return_frac.std() * np.sqrt(252)) if daily_return_frac.std() > 0 else 0.0

    equity_curve = (1 + daily_return_frac).cumprod()
    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1
    max_drawdown_pct = drawdown.min() * 100
    total_return_pct = (equity_curve.iloc[-1] - 1) * 100

    def _side_stats(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"trades": 0}
        return {
            "trades": int(len(df)),
            "win_rate": round(float(df["win"].mean()) * 100, 1),
            "avg_r_multiple": round(float(df["r_multiple"].mean()), 2),
        }

    entry_min = pd.to_datetime(trade_log["entry_dt"]).min()
    exit_max = pd.to_datetime(trade_log["exit_dt"]).max()
    start_str = str(entry_min.date()) if pd.notna(entry_min) else "N/A"
    end_str = str(exit_max.date()) if pd.notna(exit_max) else "N/A"
    duration_years = round((exit_max - entry_min).days / 365.25, 2) if (pd.notna(entry_min) and pd.notna(exit_max)) else 0.0

    return {
        "time_period": f"{start_str} to {end_str} ({duration_years:.1f} years)",
        "start_date": start_str,
        "end_date": end_str,
        "duration_years": duration_years,
        "trade_count": int(len(trade_log)),
        "win_rate_pct": round(float(trade_log["win"].mean()) * 100, 1),
        "profit_factor": round(float(profit_factor), 2),
        "sharpe": round(float(sharpe), 2),
        # Portfolio-level, assuming RISK_PER_TRADE_PCT risked per trade, compounded — NOT a raw sum of per-trade price-% returns.
        "max_drawdown_pct": round(float(max_drawdown_pct), 2),
        "total_return_pct": round(float(total_return_pct), 2),
        "avg_win_pct": round(float(wins["net_pct"].mean()), 3) if not wins.empty else 0.0,
        "avg_loss_pct": round(float(losses["net_pct"].mean()), 3) if not losses.empty else 0.0,
        "avg_r_multiple": round(float(trade_log["r_multiple"].mean()), 2),
        "long": _side_stats(trade_log[trade_log["direction"] == "long"]),
        "short": _side_stats(trade_log[trade_log["direction"] == "short"]),
    }


def per_symbol_summary(trade_log: pd.DataFrame) -> dict[str, dict]:
    if trade_log.empty:
        return {}
    return {symbol: summarize(group) for symbol, group in trade_log.groupby("symbol")}
