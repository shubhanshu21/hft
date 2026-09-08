"""
backtest/plots.py — charts for a finished backtest run, saved as PNGs
under cache/charts_<interval>m/. Matplotlib only (no display needed —
Agg backend), so this works fine over SSH / headless.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def _savefig(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_equity_curve(equity_curve: pd.DataFrame, out_path: Path, starting_capital: float) -> None:
    """Capital over time from the portfolio simulation (backtest/portfolio.py)."""
    if equity_curve.empty:
        return
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(equity_curve["timestamp"], equity_curve["capital"], color="#2563eb", linewidth=1.2)
    ax.axhline(starting_capital, color="#9ca3af", linewidth=1, linestyle="--", label="Starting capital")
    ax.set_title("Portfolio equity curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Capital (₹)")
    ax.legend()
    ax.grid(alpha=0.3)
    _savefig(fig, out_path)


def plot_drawdown(equity_curve: pd.DataFrame, out_path: Path) -> None:
    if equity_curve.empty:
        return
    running_max = equity_curve["capital"].cummax()
    drawdown_pct = (equity_curve["capital"] / running_max - 1) * 100
    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.fill_between(equity_curve["timestamp"], drawdown_pct, 0, color="#dc2626", alpha=0.4)
    ax.plot(equity_curve["timestamp"], drawdown_pct, color="#dc2626", linewidth=1)
    ax.set_title("Drawdown (%)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown %")
    ax.grid(alpha=0.3)
    _savefig(fig, out_path)


def plot_per_symbol_bars(per_symbol: dict[str, dict], out_path: Path) -> None:
    """Profit factor and win rate per symbol, side by side."""
    symbols = list(per_symbol.keys())
    if not symbols:
        return
    profit_factors = [min(per_symbol[s].get("profit_factor", 0) or 0, 5) for s in symbols]  # cap at 5 so one Infinity/huge value doesn't flatten the chart
    win_rates = [per_symbol[s].get("win_rate_pct", 0) for s in symbols]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    colors1 = ["#16a34a" if pf >= 1 else "#dc2626" for pf in profit_factors]
    ax1.bar(symbols, profit_factors, color=colors1)
    ax1.axhline(1.0, color="#374151", linewidth=1, linestyle="--")
    ax1.set_title("Profit factor by symbol (capped at 5)")
    ax1.grid(alpha=0.3, axis="y")

    colors2 = ["#16a34a" if wr >= 50 else "#dc2626" for wr in win_rates]
    ax2.bar(symbols, win_rates, color=colors2)
    ax2.axhline(50.0, color="#374151", linewidth=1, linestyle="--")
    ax2.set_title("Win rate % by symbol")
    ax2.set_ylabel("%")
    plt.setp(ax2.get_xticklabels(), rotation=45, ha="right")
    ax2.grid(alpha=0.3, axis="y")

    _savefig(fig, out_path)


def plot_trade_pnl_distribution(trade_log: pd.DataFrame, out_path: Path) -> None:
    if trade_log.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(trade_log["net_pct"], bins=60, color="#2563eb", alpha=0.8)
    ax.axvline(0, color="#dc2626", linewidth=1.2)
    ax.set_title("Trade P&L distribution (net %, after costs)")
    ax.set_xlabel("Net % per trade")
    ax.set_ylabel("Trade count")
    ax.grid(alpha=0.3)
    _savefig(fig, out_path)


def generate_all(
    trade_log: pd.DataFrame, per_symbol: dict[str, dict], equity_curve: pd.DataFrame,
    starting_capital: float, out_dir: Path,
) -> list[Path]:
    """Generate every chart for one backtest run; returns the list of file paths written."""
    paths = [
        out_dir / "equity_curve.png",
        out_dir / "drawdown.png",
        out_dir / "per_symbol.png",
        out_dir / "trade_pnl_distribution.png",
    ]
    plot_equity_curve(equity_curve, paths[0], starting_capital)
    plot_drawdown(equity_curve, paths[1])
    plot_per_symbol_bars(per_symbol, paths[2])
    plot_trade_pnl_distribution(trade_log, paths[3])
    return [p for p in paths if p.exists()]
