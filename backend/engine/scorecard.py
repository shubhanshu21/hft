"""Per-symbol paper-trading scorecard for the end-of-day Telegram summary.

Shows what decides whether a scalper makes money: win rate versus the win rate its own payoff requires. With average win W and
average loss L, break-even needs a win rate of L / (W + L); a strategy whose small breakeven-stop wins are outweighed by full
stop-outs needs a high win rate, and that gap (have vs need) is the first thing to read.
"""
from __future__ import annotations


def summarize(trades: list[dict]) -> dict:
    pnl = [float(t.get("net_pnl") or 0.0) for t in trades]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    n = len(pnl)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "n": n, "wins": len(wins), "win_pct": 100.0 * len(wins) / n if n else 0.0, "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": avg_win / avg_loss if avg_loss else float("inf") if avg_win else 0.0,
        "need_pct": 100.0 * avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) else 0.0,     # win rate required to break even at this payoff
        "pf": gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0,
        "net": sum(pnl),
    }


def by_symbol(trades: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(t["symbol"], []).append(t)
    return {sym: summarize(g) for sym, g in sorted(groups.items())}


def _row(label: str, s: dict) -> str:
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"{label:<10s}{s['n']:>3d}  {s['win_pct']:>3.0f}%/{s['need_pct']:>3.0f}%  {s['avg_win']:>6.0f}/{s['avg_loss']:<6.0f} {pf:>5s} {s['net']:>+8.0f}"


def format_scorecard(today: list[dict], history: list[dict]) -> str | None:
    """Monospace table for Telegram (HTML <pre>), or None when there is nothing to show."""
    if not history:
        return None
    header = f"{'':<10s}{'n':>3s}  {'win/need':>9s}  {'avgW/avgL':<13s}{'PF':>5s} {'net Rs':>8s}"
    lines = ["<b>SCORECARD</b> (win% vs win% needed to break even at this payoff)", "<pre>", "TODAY", header]
    lines += [_row(sym, s) for sym, s in by_symbol(today).items()] or ["(no trades)"]
    if today:
        lines.append(_row("ALL", summarize(today)))
    lines += ["", f"SINCE START ({len(history)} trades)", header]
    lines += [_row(sym, s) for sym, s in by_symbol(history).items()]
    lines.append(_row("ALL", summarize(history)))
    lines.append("</pre>")
    return "\n".join(lines)
