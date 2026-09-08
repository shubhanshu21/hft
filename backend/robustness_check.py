"""
robustness_check.py — sensitivity checks on an already-run backtest,
reusing cache/backtest_dataset.pkl (written by backtest_scalper.py) so
this doesn't rebuild years of history.

    python3 -m backtest_scalper            # once
    python3 -m robustness_check            # then, repeatedly, cheaply

Checks whether the aggregate edge reported by the main backtest survives:
  1. Excluding the single strongest-looking contributor (whichever symbol
     had the best per-symbol numbers on the smallest trade count — the
     kind of result a small sample produces by luck as often as by
     genuine edge).
  2. Tighter and looser confidence thresholds than the default 0.55/0.45.
  3. "Is ML actually helping?" — replays the SAME out-of-sample decision
     points and trade mechanics (stop/target/window), but with LightGBM's
     prediction swapped out for a trivial rule (always long, always
     short, plain momentum, coin flip). If a trivial rule matches
     LightGBM's numbers, the model isn't earning its complexity.
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np

from backtest.engine import simulate_from_dataset
from backtest.metrics import per_symbol_summary, summarize

CACHE_DIR = Path(__file__).resolve().parent / "cache"


def compare_to_baselines(full_df) -> dict:
    """Same out-of-sample rows LightGBM was scored on, same trade simulation, only the 'p_up' prediction changes."""
    oos = full_df[full_df["p_up"].notna()].copy()
    results = {}

    lightgbm_trades = simulate_from_dataset(full_df)
    results["lightgbm"] = summarize(lightgbm_trades)

    always_long = oos.copy()
    always_long["p_up"] = 1.0
    results["always_long"] = summarize(simulate_from_dataset(always_long))

    always_short = oos.copy()
    always_short["p_up"] = 0.0
    results["always_short"] = summarize(simulate_from_dataset(always_short))

    momentum_rule = oos.copy()
    momentum_rule["p_up"] = np.where(oos["mom_pct"] > 0, 1.0, 0.0)
    results["momentum_rule"] = summarize(simulate_from_dataset(momentum_rule))

    coin_flip = oos.copy()
    coin_flip["p_up"] = np.random.default_rng(42).random(len(oos))
    results["coin_flip"] = summarize(simulate_from_dataset(coin_flip))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sensitivity-check an already-run backtest.")
    parser.add_argument("--exclude", nargs="+", default=[], help="Symbol(s) to exclude in the 'excluding' variant, e.g. the strongest per-symbol performer from the main run's output.")
    parser.add_argument("--from", "--start-date", dest="start_date", default=None, help="Start date filter, e.g. 2023-01-01")
    parser.add_argument("--to", "--end-date", dest="end_date", default=None, help="End date filter, e.g. 2025-12-31")
    args = parser.parse_args()

    with open(CACHE_DIR / "backtest_dataset.pkl", "rb") as f:
        full_df = pickle.load(f)

    if args.start_date:
        start_str = str(args.start_date)[:10]
        full_df = full_df[full_df["entry_dt"].astype(str) >= start_str]
    if args.end_date:
        end_str = str(args.end_date)[:10] + " 23:59:59"
        full_df = full_df[full_df["entry_dt"].astype(str) <= end_str]

    # ANSI Colors
    C_RESET = "\033[0m"
    C_BOLD = "\033[1m"
    C_CYAN = "\033[96m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_YELLOW = "\033[93m"
    C_MAGENTA = "\033[95m"
    C_WHITE = "\033[97m"
    C_GRAY = "\033[90m"

    def _print_stat_row(label: str, s: dict) -> None:
        t_cnt = s.get("trade_count", 0)
        wr = s.get("win_rate_pct", 0.0)
        pf = s.get("profit_factor", 0.0)
        sh = s.get("sharpe", 0.0)
        ret = s.get("total_return_pct", 0.0)
        max_dd = s.get("max_drawdown_pct", 0.0)
        
        wr_col = C_GREEN if wr >= 60.0 else (C_YELLOW if wr >= 50.0 else C_RED)
        pf_col = C_GREEN if pf >= 1.20 else (C_YELLOW if pf >= 1.0 else C_RED)
        sh_col = C_GREEN if sh >= 1.0 else (C_YELLOW if sh >= 0.0 else C_RED)
        ret_col = C_GREEN if ret > 0 else C_RED

        print(f"│ {C_BOLD}{C_CYAN}{label:<22s}{C_RESET} │ {t_cnt:6d} │ {wr_col}{wr:6.1f}%{C_RESET} │ {pf_col}{pf:5.2f}{C_RESET} │ {sh_col}{sh:6.2f}{C_RESET} │ {ret_col}{ret:+8.1f}%{C_RESET} │ {C_RED}{max_dd:7.1f}%{C_RESET} │")

    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  🛡️ STRATEGY ROBUSTNESS & ML CONVICTION SENSITIVITY CHECK{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    print(f"\n{C_BOLD}{C_WHITE}┌── 🎯 CONFIDENCE THRESHOLD SENSITIVITY {'─'*45}┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Configuration':<22s} │ {'Trades':>6s} │ {'WinRate':>7s} │ {'PF':>5s} │ {'Sharpe':>6s} │ {'Return':>9s} │ {'Max DD':>8s} │{C_RESET}")
    print(f"{C_GRAY}├────────────────────────┼────────┼─────────┼───────┼────────┼───────────┼──────────┤{C_RESET}")

    baseline = simulate_from_dataset(full_df)
    _print_stat_row("Baseline (0.55/0.45)", summarize(baseline))

    for up, down in [(0.60, 0.40), (0.52, 0.48)]:
        variant = simulate_from_dataset(full_df, up_threshold=up, down_threshold=down)
        _print_stat_row(f"Threshold ({up:.2f}/{down:.2f})", summarize(variant))

    if args.exclude:
        excluded = simulate_from_dataset(full_df, exclude_symbols=tuple(args.exclude))
        _print_stat_row(f"Excl. {','.join(args.exclude)[:12]}", summarize(excluded))

    print(f"{C_BOLD}{C_WHITE}└────────────────────────┴────────┴─────────┴───────┴────────┴───────────┴──────────┘{C_RESET}")

    print(f"\n{C_BOLD}{C_MAGENTA}┌── 🧠 IS ML ACTUALLY HELPING? (LightGBM vs. Trivial Controls) ──────────────┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Decision Engine':<22s} │ {'Trades':>6s} │ {'WinRate':>7s} │ {'PF':>5s} │ {'Sharpe':>6s} │ {'Return':>9s} │ {'Max DD':>8s} │{C_RESET}")
    print(f"{C_GRAY}├────────────────────────┼────────┼─────────┼───────┼────────┼───────────┼──────────┤{C_RESET}")

    baselines = compare_to_baselines(full_df)
    labels = {
        "lightgbm": "1. LightGBM (Model)",
        "always_long": "2. Always Long (Naive)",
        "always_short": "3. Always Short (Naive)",
        "momentum_rule": "4. Simple Momentum",
        "coin_flip": "5. Random Coin Flip"
    }
    for name, stats in baselines.items():
        _print_stat_row(labels.get(name, name), stats)

    print(f"{C_BOLD}{C_MAGENTA}└────────────────────────┴────────┴─────────┴───────┴────────┴───────────┴──────────┘{C_RESET}\n")


if __name__ == "__main__":
    main()
