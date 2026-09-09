"""
backtest_scalper.py — CLI: backtest the LightGBM direction-scalper across
the curated NIFTY50/BankNifty universe on the local native-5-minute
archive (backend/archive/*.csv, 2015-2026 — see data/local_5min_archive.py).
No Upstox API calls or login needed for this — see update_archive.py to
top up the archive with recent Upstox data first if it's gone stale.

    python3 -m backtest_scalper
    python3 -m backtest_scalper --symbols HDFCBANK ICICIBANK

Writes cache/backtest_<symbol>.json per symbol and cache/backtest_summary.json
for the pooled result, and prints a summary table. No orders are placed —
this only reads historical candles.
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
from pathlib import Path

import pandas as pd

from backtest.engine import run_backtest
from backtest.metrics import per_symbol_summary, summarize
from backtest.plots import generate_all as generate_all_charts
from backtest.portfolio import STARTING_CAPITAL, simulate_portfolio, summarize_portfolio
from data.local_5min_archive import available_symbols
from strategy.risk import DEFAULT_LEVERAGE, DEFAULT_RISK_PCT
from strategy.screener import DEFAULT_TOP_N, build_daily_universe
from strategy.universe import CURATED_SYMBOLS
from utils.logger import get_logger, setup_logger

CACHE_DIR = Path(__file__).resolve().parent / "cache"


def run_scalper_backtest(
    symbols: list[str] | None = None,
    capital: float = STARTING_CAPITAL,
    risk_pct: float = DEFAULT_RISK_PCT,
    leverage: float = DEFAULT_LEVERAGE,
    top_n: int = DEFAULT_TOP_N,
    no_screener: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    target_mult: float = 1.0,
    be_mult: float = 0.35,
    no_meta_label: bool = False,
    long_only: bool = False,
    direction: str = "both",
    trade_only: list[str] | None = None,
) -> dict:
    setup_logger("", log_file=str(Path(__file__).resolve().parent / "logs" / "backtest_scalper.log"))
    log = get_logger(__name__)
    logging.getLogger("pandas_ta_classic").setLevel(logging.ERROR)

    if symbols is not None:
        target_symbols, daily_universe = symbols, None
        archived = available_symbols()
        missing_from_archive = sorted(s for s in target_symbols if s not in archived)
        if missing_from_archive:
            log.warning(
                "--symbols contains symbol(s) with no local archive data — they will produce zero trades: %s. "
                "Run update_archive.py to top up, or check for demerged/renamed tickers.",
                missing_from_archive,
            )
    elif no_screener:
        target_symbols, daily_universe = CURATED_SYMBOLS, None
    else:
        all_symbols = sorted(available_symbols())
        log.info("Building daily 'stocks in play' universe from %d archive symbols (top %d/day)...", len(all_symbols), top_n)
        daily_universe = build_daily_universe(all_symbols, top_n=top_n)
        target_symbols = sorted({s for day_symbols in daily_universe.values() for s in day_symbols})
        log.info("Screener selected %d distinct symbols across the whole history.", len(target_symbols))

    direction_filter = "long" if long_only else direction
    trade_only_symbols = set(trade_only) if trade_only else None

    trade_log, dataset = run_backtest(
        target_symbols,
        daily_universe,
        use_meta_label=not no_meta_label,
        trade_only_symbols=trade_only_symbols,
    )
    if trade_log.empty:
        log.warning("No trades produced — check date range / universe resolution / instrument history availability.")
        return {}

    if direction_filter != "both":
        log.info("Filtering trade log to direction: %s", direction_filter.upper())
        trade_log = trade_log[trade_log["direction"] == direction_filter]

    if from_date:
        start_str = str(from_date)[:10]
        trade_log = trade_log[trade_log["entry_dt"].astype(str) >= start_str]
    if to_date:
        end_str = str(to_date)[:10] + " 23:59:59"
        trade_log = trade_log[trade_log["entry_dt"].astype(str) <= end_str]

    if trade_log.empty:
        log.warning("No trades found within specified criteria.")
        return {}

    CACHE_DIR.mkdir(exist_ok=True)
    charts_dir = CACHE_DIR / "charts"
    charts_dir.mkdir(exist_ok=True)

    for old_json in CACHE_DIR.glob("backtest_*.json"):
        try:
            old_json.unlink()
        except OSError:
            pass
    for old_chart in charts_dir.glob("*.png"):
        try:
            old_chart.unlink()
        except OSError:
            pass

    with open(CACHE_DIR / "backtest_dataset.pkl", "wb") as f:
        pickle.dump(dataset, f)
    trade_log.to_csv(CACHE_DIR / "backtest_trades.csv", index=False)

    per_symbol = per_symbol_summary(trade_log)
    for symbol, stats in per_symbol.items():
        (CACHE_DIR / f"backtest_{symbol.lower()}.json").write_text(json.dumps(stats, indent=2, default=str))

    aggregate = summarize(trade_log)

    executed, equity_curve = simulate_portfolio(trade_log, starting_capital=capital, risk_pct=risk_pct, leverage=leverage)
    portfolio_stats = summarize_portfolio(executed, equity_curve, starting_capital=capital)
    portfolio_stats["trades_skipped_capital_or_concurrency"] = len(trade_log) - len(executed)
    portfolio_stats["leverage_used"] = leverage
    portfolio_stats["risk_pct_used"] = risk_pct
    equity_curve.to_csv(CACHE_DIR / "backtest_equity_curve.csv", index=False)

    (CACHE_DIR / "backtest_summary.json").write_text(json.dumps(
        {"aggregate": aggregate, "per_symbol": per_symbol, "portfolio": portfolio_stats}, indent=2, default=str,
    ))

    # ANSI Color Helpers
    C_RESET = "\033[0m"
    C_BOLD = "\033[1m"
    C_CYAN = "\033[96m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_YELLOW = "\033[93m"
    C_BLUE = "\033[94m"
    C_MAGENTA = "\033[95m"
    C_WHITE = "\033[97m"
    C_GRAY = "\033[90m"

    def _col_wr(wr: float) -> str:
        color = C_GREEN if wr >= 60.0 else (C_YELLOW if wr >= 50.0 else C_RED)
        return f"{color}{wr:5.1f}%{C_RESET}"

    def _col_ret(val: float, is_pct: bool = True) -> str:
        color = C_GREEN if val > 0 else (C_RED if val < 0 else C_WHITE)
        unit = "%" if is_pct else ""
        return f"{color}{val:+8.2f}{unit}{C_RESET}"

    def _col_pf(pf: float) -> str:
        color = C_GREEN if pf >= 1.20 else (C_YELLOW if pf >= 1.0 else C_RED)
        return f"{color}{pf:5.2f}{C_RESET}"

    t_period = portfolio_stats.get("time_period", "N/A")
    final_cap = portfolio_stats.get("final_capital", capital)
    tot_ret = portfolio_stats.get("total_return_pct", 0.0)
    max_dd = portfolio_stats.get("max_drawdown_pct", 0.0)
    trades_taken = portfolio_stats.get("trades_taken", 0)
    skipped = portfolio_stats.get("trades_skipped_capital_or_concurrency", 0)

    # Header Banner
    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  ⚡ 5-MINUTE INTRADAY SCALPER — WALK-FORWARD BACKTEST RESULTS{C_RESET}")
    print(f"  {C_GRAY}📅 Period:{C_RESET} {C_YELLOW}{t_period}{C_RESET} | {C_GRAY}💰 Capital:{C_RESET} {C_WHITE}₹{capital:,.0f}{C_RESET} | {C_GRAY}⚙️ Leverage:{C_RESET} {C_MAGENTA}{leverage:.1f}x{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    # Portfolio Summary Card
    cap_color = C_GREEN if tot_ret > 0 else C_RED
    print(f"\n{C_BOLD}{C_WHITE}┌── 📊 PORTFOLIO EXECUTIVE SUMMARY ──────────────────────────────────────────────────┐{C_RESET}")
    print(f"│  {C_GRAY}Starting Capital:{C_RESET}  {C_WHITE}₹{capital:,.2f}{C_RESET}          {C_GRAY}Final Capital:{C_RESET}    {cap_color}₹{final_cap:,.2f} ({tot_ret:+.2f}%){C_RESET}")
    print(f"│  {C_GRAY}Executed Trades:{C_RESET}   {C_WHITE}{trades_taken:,}{C_RESET}                {C_GRAY}Skipped (Cap/Slot):{C_RESET} {C_YELLOW}{skipped:,}{C_RESET}")
    print(f"│  {C_GRAY}Max Drawdown:{C_RESET}      {C_RED}{max_dd:.2f}%{C_RESET}               {C_GRAY}Leverage Multiplier:{C_RESET}{C_MAGENTA}{leverage:.1f}x (SEBI MIS){C_RESET}")
    print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    # Itemized Brokerage, Taxes & Friction Breakdown Table
    gross_pnl_val = portfolio_stats.get("gross_pnl_rupees", 0.0)
    brok_val = portfolio_stats.get("total_brokerage_rupees", 0.0)
    stt_val = portfolio_stats.get("total_stt_rupees", 0.0)
    stamp_val = portfolio_stats.get("total_stamp_duty_rupees", 0.0)
    exch_val = portfolio_stats.get("total_exchange_txn_rupees", 0.0)
    sebi_val = portfolio_stats.get("total_sebi_charges_rupees", 0.0)
    gst_val = portfolio_stats.get("total_gst_rupees", 0.0)
    slip_val = portfolio_stats.get("total_slippage_rupees", 0.0)
    tot_charges_val = portfolio_stats.get("total_friction_and_taxes_rupees", 0.0)
    net_realized_pnl = gross_pnl_val - tot_charges_val

    gross_color = C_GREEN if gross_pnl_val > 0 else C_RED
    net_color = C_GREEN if net_realized_pnl > 0 else C_RED

    print(f"\n{C_BOLD}{C_YELLOW}┌── 💰 ITEMIZED BROKERAGE, TAXES & FRICTION BREAKDOWN (₹ INR) {'─'*22}┐{C_RESET}")
    print(f"│  {C_GRAY}Gross Trading PnL:{C_RESET}               {gross_color}₹{gross_pnl_val:+12,.2f}{C_RESET}                                    │")
    print(f"│  {C_GRAY}Upstox Brokerage (₹20/ord cap):{C_RESET}  {C_RED}-₹{brok_val:12,.2f}{C_RESET}  {C_GRAY}(Includes 18% GST on brokerage){C_RESET}  │")
    print(f"│  {C_GRAY}Securities Trans. Tax (STT):{C_RESET}     {C_RED}-₹{stt_val:12,.2f}{C_RESET}  {C_GRAY}(0.025% on Sell side){C_RESET}            │")
    print(f"│  {C_GRAY}Stamp Duty (Maharashtra rate):{C_RESET}   {C_RED}-₹{stamp_val:12,.2f}{C_RESET}  {C_GRAY}(0.003% on Buy side){C_RESET}             │")
    print(f"│  {C_GRAY}Exchange Turnover Charges:{C_RESET}       {C_RED}-₹{exch_val:12,.2f}{C_RESET}  {C_GRAY}(NSE 0.00297% turnover fee){C_RESET}      │")
    print(f"│  {C_GRAY}SEBI Regulatory Turnover Fee:{C_RESET}    {C_RED}-₹{sebi_val:12,.2f}{C_RESET}  {C_GRAY}(₹10 per Crore turnover){C_RESET}         │")
    print(f"│  {C_GRAY}Estimated Bid-Ask Slippage:{C_RESET}      {C_RED}-₹{slip_val:12,.2f}{C_RESET}  {C_GRAY}(½-tick spread + impact){C_RESET}         │")
    print(f"{C_GRAY}├──────────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
    print(f"│  {C_BOLD}{C_WHITE}TOTAL CHARGES & TAXES DEDUCTED:{C_RESET}  {C_RED}{C_BOLD}-₹{tot_charges_val:12,.2f}{C_RESET}                                    │")
    print(f"│  {C_BOLD}{C_WHITE}NET REALIZED PROFIT IN ₹:{C_RESET}        {net_color}{C_BOLD}₹{net_realized_pnl:+12,.2f}{C_RESET}  {C_GRAY}(After all friction & fees){C_RESET} │")
    print(f"{C_BOLD}{C_YELLOW}└──────────────────────────────────────────────────────────────────────────────────┘{C_RESET}")

    # Per-Symbol Performance Table
    print(f"\n{C_BOLD}{C_CYAN}┌── 📈 PER-SYMBOL ALPHA BREAKDOWN {'─'*51}┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Symbol':<12s} │ {'Trades':>6s} │ {'Win Rate':>8s} │ {'Long Win':>8s} │ {'Short Win':>9s} │ {'PF':>5s} │ {'Sharpe':>6s} │ {'Net Return':>10s} │{C_RESET}")
    print(f"{C_GRAY}├──────────────┼────────┼──────────┼──────────┼───────────┼───────┼────────┼────────────┤{C_RESET}")

    for symbol, stats in per_symbol.items():
        t_cnt = stats.get("trade_count", 0)
        wr = stats.get("win_rate_pct", 0.0)
        long_wr = stats.get("long", {}).get("win_rate", 0.0)
        short_wr = stats.get("short", {}).get("win_rate", 0.0)
        pf = stats.get("profit_factor", 0.0)
        sharpe = stats.get("sharpe", 0.0)
        ret = stats.get("total_return_pct", 0.0)
        sh_color = C_GREEN if sharpe >= 1.5 else (C_YELLOW if sharpe >= 0 else C_RED)

        print(f"│ {C_BOLD}{C_CYAN}{symbol:<12s}{C_RESET} │ {t_cnt:6d} │ {_col_wr(wr):>8s} │ {_col_wr(long_wr):>8s} │ {_col_wr(short_wr):>9s} │ {_col_pf(pf):>5s} │ {sh_color}{sharpe:6.2f}{C_RESET} │ {_col_ret(ret):>10s} │")

    print(f"{C_BOLD}{C_CYAN}└──────────────┴────────┴──────────┴──────────┴───────────┴───────┴────────┴────────────┘{C_RESET}")

    # Brokerage-adjusted aggregate
    if not executed.empty:
        from backtest.metrics import summarize as _summarize
        exec_adj = executed.copy()
        trade_value = exec_adj["qty"] * exec_adj["entry_price"]
        brok_pct = exec_adj["brokerage_rupees"] / trade_value * 100
        exec_adj["net_pct"] = exec_adj["net_pct"] - brok_pct
        exec_adj["win"] = exec_adj["net_pct"] > 0
        stop_dist = (exec_adj["entry_price"] - exec_adj["stop"]).abs().clip(lower=1e-9)
        exec_adj["r_multiple"] = exec_adj["net_pct"] / 100 * exec_adj["entry_price"] / stop_dist
        aggregate_adj = _summarize(exec_adj)

        agg_wr = aggregate_adj.get("win_rate_pct", 0.0)
        agg_pf = aggregate_adj.get("profit_factor", 0.0)
        agg_sh = aggregate_adj.get("sharpe", 0.0)
        long_wr = aggregate_adj.get("long", {}).get("win_rate", 0.0)
        short_wr = aggregate_adj.get("short", {}).get("win_rate", 0.0)

        print(f"\n{C_BOLD}{C_WHITE}┌── 🏆 BROKERAGE & TAX ADJUSTED OVERALL STATS ──────────────────────────────────────┐{C_RESET}")
        print(f"│  {C_GRAY}Executed Trades:{C_RESET} {C_WHITE}{len(exec_adj):,}{C_RESET}       {C_GRAY}Overall Win Rate:{C_RESET} {_col_wr(agg_wr)}  ({C_GREEN}Long: {long_wr:.1f}%{C_RESET} | {C_BLUE}Short: {short_wr:.1f}%{C_RESET})")
        print(f"│  {C_GRAY}Profit Factor:{C_RESET}   {_col_pf(agg_pf)}        {C_GRAY}Sharpe Ratio:{C_RESET}     {C_GREEN if agg_sh >= 1.0 else C_YELLOW}{agg_sh:.2f}{C_RESET}")
        print(f"│  {C_GRAY}Avg Win / Loss:{C_RESET}  {C_GREEN}+{aggregate_adj.get('avg_win_pct', 0):.2f}%{C_RESET} / {C_RED}{aggregate_adj.get('avg_loss_pct', 0):.2f}%{C_RESET}     {C_GRAY}Avg R-Multiple:{C_RESET} {C_WHITE}{aggregate_adj.get('avg_r_multiple', 0):+.2f}R{C_RESET}")
        print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    charts_dir = CACHE_DIR / "charts"
    chart_paths = generate_all_charts(trade_log, per_symbol, equity_curve, capital, charts_dir)
    print(f"\n{C_BOLD}{C_MAGENTA}🎨 Generated Charts ({charts_dir}/):{C_RESET}")
    for p in chart_paths:
        print(f"  {C_GREEN}✔{C_RESET} {C_WHITE}{p.name}{C_RESET}")

    return {
        "aggregate": aggregate,
        "per_symbol": per_symbol,
        "portfolio": portfolio_stats,
        "executed": executed,
        "equity_curve": equity_curve,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the LightGBM direction-scalper.")
    parser.add_argument("--symbols", nargs="+", default=None, help="Fixed symbol list — overrides the daily screener if given")
    parser.add_argument("--no-screener", action="store_true", help="Use the fixed curated list instead of the daily 'stocks in play' screener")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="How many symbols the daily screener picks per day")
    parser.add_argument("--capital", type=float, default=STARTING_CAPITAL, help="Starting capital (₹) for the capital-aware position-sizing pass")
    parser.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT, help="Risk percentage of capital per trade (e.g. 4.0 for 4%% risk per trade)")
    parser.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE, help="Intraday leverage multiplier (e.g. 1.0 for pure cash / zero leverage, or 4.0 for MIS margin)")
    parser.add_argument("--from", "--start-date", dest="start_date", default=None, help="Start date for backtesting, e.g. 2023-01-01")
    parser.add_argument("--to", "--end-date", dest="end_date", default=None, help="End date for backtesting, e.g. 2025-12-31")
    parser.add_argument("--target-mult", type=float, default=1.0, help="Take profit target multiple of stop distance (e.g. 1.0 for 1:1, 1.5 for 1:1.5)")
    parser.add_argument("--be-mult", type=float, default=0.35, help="Breakeven trigger multiple of stop distance (e.g. 0.35 for +0.35R)")
    parser.add_argument("--no-meta-label", action="store_true", help="Skip the meta-labeling filter stage — recommended for high-frequency micro-scalping to capture small rapid intraday moves")
    parser.add_argument("--long-only", action="store_true", help="Take LONG trades only (skip all short setups)")
    parser.add_argument("--direction", choices=["both", "long", "short"], default="both", help="Trade direction filter: both, long, or short")
    parser.add_argument("--trade-only", nargs="+", default=None, help="Train on --symbols (pooled, for enough data) but only ever take trades on this subset — e.g. --symbols HDFCBANK ICICIBANK AXISBANK SBIN --trade-only HDFCBANK")
    args = parser.parse_args()

    run_scalper_backtest(
        symbols=args.symbols,
        capital=args.capital,
        risk_pct=args.risk_pct,
        leverage=args.leverage,
        top_n=args.top_n,
        no_screener=args.no_screener,
        from_date=args.start_date,
        to_date=args.end_date,
        target_mult=args.target_mult,
        be_mult=args.be_mult,
        no_meta_label=args.no_meta_label,
        long_only=args.long_only,
        direction=args.direction,
        trade_only=args.trade_only,
    )


if __name__ == "__main__":
    main()
