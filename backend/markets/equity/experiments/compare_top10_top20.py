#!/usr/bin/env python3
import sys
import pandas as pd
from markets.equity.experiments.fibonacci_retracement import run_fibonacci_backtest, get_symbol_file

top10 = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
    'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA'
]

top20 = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
    'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA',
    'IOC', 'SBIN', 'ULTRACEMCO', 'SBILIFE', 'ASIANPAINT', 
    'COALINDIA', 'HEROMOTOCO', 'ONGC', 'LT', 'GRASIM'
]

def evaluate(syms, label):
    trades_all = []
    for sym in syms:
        f = get_symbol_file(sym, 'daily')
        if not f:
            continue
        stats, trades = run_fibonacci_backtest(f, sym, 'daily', initial_capital=100000.0, risk_pct=5.0, target_type='rr2')
        for t in trades:
            trades_all.append({
                'sym': sym,
                'exit': str(t.exit_time),
                'net': t.net_pnl,
                'friction': t.friction
            })

    trades_all.sort(key=lambda t: t['exit'])
    t2026 = [t for t in trades_all if t['exit'][:4] == '2026']

    # Compounding all time
    c_all = 100000.0
    for t in trades_all:
        c_all += c_all * (t['net'] / 100000.0)

    # Compounding 2026
    c_2026 = 100000.0
    for t in t2026:
        c_2026 += c_2026 * (t['net'] / 100000.0)

    w_all = sum(1 for t in trades_all if t['net'] > 0)
    w_2026 = sum(1 for t in t2026 if t['net'] > 0)

    return {
        'Universe': label,
        'All Trades': len(trades_all),
        'All WR': f"{(w_all / len(trades_all) * 100):.1f}%" if trades_all else "0%",
        'All Net (₹)': f"₹{sum(t['net'] for t in trades_all):,.2f}",
        'All Comp (₹)': f"₹{c_all:,.2f}",
        '2026 Trades': len(t2026),
        '2026 WR': f"{(w_2026 / len(t2026) * 100):.1f}%" if t2026 else "0%",
        '2026 Net (₹)': f"₹{sum(t['net'] for t in t2026):,.2f}",
        '2026 Comp (₹)': f"₹{c_2026:,.2f}",
    }

r10 = evaluate(top10, "Top 10 Stocks")
r20 = evaluate(top20, "Top 20 Stocks")

df = pd.DataFrame([r10, r20])
print(df.to_string(index=False))
