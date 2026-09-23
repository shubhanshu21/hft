"""
utils/chart.py — Portfolio equity curve chart from database.py's portfolio_snapshots.

Headless (Agg backend) since this runs on a server with no display attached.
"""

from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker


def generate_equity_curve(snapshots: list[dict], account_id: str, out_path: Path) -> Path | None:
    """
    Renders the equity curve to out_path, one point per calendar day (the
    day's end-of-day capital, i.e. its last snapshot -- portfolio_snapshots
    has one row per trade close, which is too granular to read once this
    spans many trading days) from the first trading day through today.
    Returns None if there's nothing to plot yet.
    """
    if not snapshots:
        return None

    # snapshots are ordered oldest -> newest (see database.py's get_snapshots),
    # so the last write per date naturally ends up as that day's EOD value.
    daily: dict[str, dict] = {s["date"]: s for s in snapshots}
    dates_sorted = sorted(daily.keys())
    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates_sorted]
    capital = [daily[d]["current_capital"] for d in dates_sorted]
    starting = snapshots[0]["starting_capital"]

    up = capital[-1] >= starting
    color = "#22c55e" if up else "#ef4444"

    fig, ax = plt.subplots(figsize=(10, 5), dpi=130)
    ax.plot(dates, capital, color=color, linewidth=1.8, marker="o", markersize=3)
    ax.axhline(starting, color="#888888", linestyle="--", linewidth=1,
               label=f"Starting Capital ₹{starting:,.0f}")
    ax.fill_between(dates, capital, starting, color=color, alpha=0.12)

    final = capital[-1]
    pct = (final - starting) / starting * 100 if starting else 0.0
    span = f"{dates[0].isoformat()} to {dates[-1].isoformat()}" if len(dates) > 1 else dates[0].isoformat()
    ax.set_title(f"Portfolio Equity — {account_id}  (₹{final:,.0f}, {pct:+.2f}%)\n{span}", fontsize=12)
    ax.set_ylabel("Capital (₹)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b"))
    if len(dates) == 1:
        from datetime import timedelta
        ax.set_xlim(dates[0] - timedelta(days=3), dates[0] + timedelta(days=3))
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path
