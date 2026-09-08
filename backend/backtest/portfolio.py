"""
backtest/portfolio.py — replay the already-decided trade log (from
backtest/engine.py) against a single real capital account, sized via
strategy/risk.py, so the backtest reflects what an actual account could
have taken: never more shares than capital affords, never more risk per
trade than the risk cap, never more than MAX_CONCURRENT_POSITIONS open
at once across the whole universe.

This runs AFTER engine.simulate_from_dataset — that stage decides
per-symbol WHICH signals would be taken (one at a time per symbol);
this stage decides, across ALL symbols together in real chronological
order, which of those get capital and how many shares, given the
account's actual size at that moment. A signal can still go untaken
here if capital is fully committed to other open positions or too small
to buy even one share within the risk cap — that's the "don't go
bankrupt" guarantee: sizing is never invented, only ever computed from
capital that's actually free.
"""
from __future__ import annotations

import math

import pandas as pd

from strategy import costs
from strategy.risk import DEFAULT_RISK_PCT, INTRADAY_EQUITY_LEVERAGE, MAX_CONCURRENT_POSITIONS

STARTING_CAPITAL = 100_000.0  # ₹1 lakh, a plausible small-retail-account default


def simulate_portfolio(
    trade_log: pd.DataFrame,
    starting_capital: float = STARTING_CAPITAL,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_concurrent: int = MAX_CONCURRENT_POSITIONS,
    leverage: float = INTRADAY_EQUITY_LEVERAGE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (executed_trades, equity_curve). `executed_trades` is a
    subset of `trade_log` (only the ones capital/concurrency allowed),
    each annotated with `qty` (shares) and `pnl_rupees`. `equity_curve`
    is (timestamp, capital) after every realized (exited) trade.
    Positions are sized using SEBI standard 5x MIS intraday leverage (20% margin).
    """
    if trade_log.empty:
        return pd.DataFrame(), pd.DataFrame(columns=["timestamp", "capital"])

    candidates = trade_log.sort_values("entry_dt").reset_index(drop=True)

    capital = starting_capital
    reserved_margin = 0.0
    open_positions: list[dict] = []  # {"exit_dt", "qty", "entry_price", "net_pct"}
    executed = []
    equity_points = [(candidates["entry_dt"].iloc[0], capital)]

    def _settle_due(now: pd.Timestamp) -> None:
        nonlocal capital, reserved_margin, open_positions
        due = [p for p in open_positions if p["exit_dt"] <= now]
        still_open = [p for p in open_positions if p["exit_dt"] > now]
        for pos in sorted(due, key=lambda p: p["exit_dt"]):
            dir_mult = 1 if pos.get("direction", "long") == "long" else -1
            entry_val = pos["qty"] * pos["entry_price"]
            exit_val = pos["qty"] * pos["exit_price"]
            # Gross PnL (raw price move × qty)
            gross_pnl = pos["qty"] * (pos["exit_price"] - pos["entry_price"]) * dir_mult
            # Fully itemized costs — every fee deducted once (no double-count)
            brokerage = costs.brokerage_rupees(entry_val) + costs.brokerage_rupees(exit_val)
            stt = (exit_val if dir_mult == 1 else entry_val) * (costs.STT_PCT_SELL_SIDE / 100)
            stamp = (entry_val if dir_mult == 1 else exit_val) * (costs.STAMP_DUTY_PCT_BUY_SIDE / 100)
            exchange = (entry_val + exit_val) * (costs.EXCHANGE_TXN_PCT_PER_SIDE / 100)
            sebi = (entry_val + exit_val) * (costs.SEBI_PCT_PER_SIDE / 100)
            slip = (entry_val + exit_val) * (costs.slippage_pct_per_leg(pos["entry_price"]) / 100)
            total_costs = brokerage + stt + stamp + exchange + sebi + slip
            net_pnl = gross_pnl - total_costs  # true net after ALL fees
            capital += net_pnl
            margin_used = entry_val / leverage
            reserved_margin -= margin_used
            reserved_margin = max(0.0, reserved_margin)
            equity_points.append((pos["exit_dt"], capital))
        open_positions = still_open

    for _, row in candidates.iterrows():
        _settle_due(row["entry_dt"])

        if len(open_positions) >= max_concurrent or capital <= 1000:
            continue  # no free slot right now or depleted capital

        free_margin = max(0.0, capital - reserved_margin)
        per_share_risk = abs(row["entry_price"] - row["stop"])
        if per_share_risk <= 0 or capital <= 0:
            continue
        qty_by_risk = math.floor(capital * risk_pct / 100 / per_share_risk)
        qty_by_margin = math.floor(free_margin * leverage / row["entry_price"]) if row["entry_price"] > 0 else 0
        qty = max(0, min(qty_by_risk, qty_by_margin))
        if qty == 0:
            continue  # capital too small (or too committed) to take this trade at all

        margin_req = qty * row["entry_price"] / leverage
        reserved_margin += margin_req
        open_positions.append({
            "exit_dt": row["exit_dt"], "qty": qty, "entry_price": row["entry_price"],
            "exit_price": row["exit_price"], "net_pct": row["net_pct"],
            "direction": row["direction"],  # needed for correct STT/stamp per leg in _settle_due
        })

        entry_val = qty * row["entry_price"]
        exit_val = qty * row["exit_price"]
        dir_mult = 1 if row["direction"] == "long" else -1

        # Itemized Upstox Brokerage & Statutory Taxes in ₹
        brokerage = costs.brokerage_rupees(entry_val) + costs.brokerage_rupees(exit_val)
        stt = (exit_val if dir_mult == 1 else entry_val) * (costs.STT_PCT_SELL_SIDE / 100)
        stamp_duty = (entry_val if dir_mult == 1 else exit_val) * (costs.STAMP_DUTY_PCT_BUY_SIDE / 100)
        exchange_txn = (entry_val + exit_val) * (costs.EXCHANGE_TXN_PCT_PER_SIDE / 100)
        sebi_fees = (entry_val + exit_val) * (costs.SEBI_PCT_PER_SIDE / 100)
        gst_total = (brokerage * costs.GST_RATE / (1 + costs.GST_RATE)) + (exchange_txn * costs.GST_RATE)
        slippage = (entry_val + exit_val) * (costs.slippage_pct_per_leg(row["entry_price"]) / 100)

        gross_pnl = qty * (row["exit_price"] - row["entry_price"]) * dir_mult
        total_charges = brokerage + stt + stamp_duty + exchange_txn + sebi_fees + slippage
        net_pnl = gross_pnl - total_charges

        trade = row.to_dict()
        trade["qty"] = qty
        trade["trade_value"] = entry_val
        trade["capital_before"] = capital
        trade["gross_pnl_rupees"] = gross_pnl
        trade["brokerage_rupees"] = brokerage
        trade["stt_rupees"] = stt
        trade["stamp_duty_rupees"] = stamp_duty
        trade["exchange_txn_rupees"] = exchange_txn
        trade["sebi_charges_rupees"] = sebi_fees
        trade["gst_rupees"] = gst_total
        trade["slippage_rupees"] = slippage
        trade["total_friction_rupees"] = total_charges
        trade["pnl_rupees"] = net_pnl
        executed.append(trade)

    # settle whatever's still open at the very end of the backtest
    if open_positions:
        _settle_due(max(p["exit_dt"] for p in open_positions))

    equity_curve = pd.DataFrame(equity_points, columns=["timestamp", "capital"]).drop_duplicates("timestamp", keep="last")
    return pd.DataFrame(executed), equity_curve


def summarize_portfolio(executed: pd.DataFrame, equity_curve: pd.DataFrame, starting_capital: float = STARTING_CAPITAL) -> dict:
    if executed.empty:
        return {"trades_taken": 0, "final_capital": starting_capital, "total_return_pct": 0.0}

    # Final capital = starting + sum of all net PnL after every fee (authoritative single source of truth)
    net_pnl_sum = executed["pnl_rupees"].sum() if "pnl_rupees" in executed.columns else 0.0
    final_capital = starting_capital + net_pnl_sum
    # Equity curve used for drawdown calculation; re-anchor it from starting_capital so it's consistent
    if not equity_curve.empty:
        # Shift the equity curve so it starts exactly at starting_capital (fixes any initialization drift)
        curve_start = equity_curve["capital"].iloc[0]
        equity_curve = equity_curve.copy()
        equity_curve["capital"] = equity_curve["capital"] - curve_start + starting_capital
    running_max = equity_curve["capital"].cummax() if not equity_curve.empty else pd.Series([starting_capital])
    drawdown_pct = (equity_curve["capital"] / running_max - 1) * 100 if not equity_curve.empty else pd.Series([0.0])
    ever_wiped_out = bool((equity_curve["capital"] <= 0).any()) if not equity_curve.empty else False

    entry_min = pd.to_datetime(executed["entry_dt"]).min()
    exit_max = pd.to_datetime(executed["exit_dt"]).max()
    start_str = str(entry_min.date()) if pd.notna(entry_min) else "N/A"
    end_str = str(exit_max.date()) if pd.notna(exit_max) else "N/A"
    duration_years = round((exit_max - entry_min).days / 365.25, 2) if (pd.notna(entry_min) and pd.notna(exit_max)) else 0.0

    gross_pnl_sum = executed["gross_pnl_rupees"].sum() if "gross_pnl_rupees" in executed.columns else 0.0
    brokerage_sum = executed["brokerage_rupees"].sum() if "brokerage_rupees" in executed.columns else 0.0
    stt_sum = executed["stt_rupees"].sum() if "stt_rupees" in executed.columns else 0.0
    stamp_duty_sum = executed["stamp_duty_rupees"].sum() if "stamp_duty_rupees" in executed.columns else 0.0
    exchange_txn_sum = executed["exchange_txn_rupees"].sum() if "exchange_txn_rupees" in executed.columns else 0.0
    sebi_sum = executed["sebi_charges_rupees"].sum() if "sebi_charges_rupees" in executed.columns else 0.0
    gst_sum = executed["gst_rupees"].sum() if "gst_rupees" in executed.columns else 0.0
    slippage_sum = executed["slippage_rupees"].sum() if "slippage_rupees" in executed.columns else 0.0
    total_charges_sum = executed["total_friction_rupees"].sum() if "total_friction_rupees" in executed.columns else 0.0

    return {
        "time_period": f"{start_str} to {end_str} ({duration_years:.1f} years)",
        "start_date": start_str,
        "end_date": end_str,
        "duration_years": duration_years,
        "starting_capital": starting_capital,
        "final_capital": round(float(final_capital), 2),
        "total_return_pct": round((final_capital / starting_capital - 1) * 100, 2),
        "gross_pnl_rupees": round(float(gross_pnl_sum), 2),
        "total_brokerage_rupees": round(float(brokerage_sum), 2),
        "total_stt_rupees": round(float(stt_sum), 2),
        "total_stamp_duty_rupees": round(float(stamp_duty_sum), 2),
        "total_exchange_txn_rupees": round(float(exchange_txn_sum), 2),
        "total_sebi_charges_rupees": round(float(sebi_sum), 2),
        "total_gst_rupees": round(float(gst_sum), 2),
        "total_slippage_rupees": round(float(slippage_sum), 2),
        "total_friction_and_taxes_rupees": round(float(total_charges_sum), 2),
        "trades_taken": int(len(executed)),
        "trades_skipped_capital_or_concurrency": None,  # filled in by caller, which knows the candidate count
        "max_drawdown_pct": round(float(drawdown_pct.min()), 2) if not drawdown_pct.empty else 0.0,
        "ever_hit_zero_or_below": ever_wiped_out,
        "avg_qty_per_trade": round(float(executed["qty"].mean()), 1) if "qty" in executed.columns else 0.0,
    }
