#!/usr/bin/env python3
import sys
import pandas as pd
from markets.equity.experiments.fibonacci_retracement import run_fibonacci_backtest, get_symbol_file

top_stocks = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 
    'HINDALCO', 'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 
    'BHARTIARTL', 'SUNPHARMA'
]

all_trades = []
for sym in top_stocks:
    f = get_symbol_file(sym, 'daily')
    if not f: continue
    stats, trades = run_fibonacci_backtest(f, sym, 'daily', initial_capital=100000.0, risk_pct=5.0, target_type='rr2')
    all_trades.extend(trades)

all_trades.sort(key=lambda t: t.exit_time)

capital = 100000.0
peak = capital
max_dd = 0.0
yearly_comp = {}

for t in all_trades:
    exit_yr = str(t.exit_time)[:4]
    if exit_yr not in yearly_comp:
        yearly_comp[exit_yr] = {'start': capital, 'end': capital, 'trades': 0, 'wins': 0}
    
    pct_pnl = t.net_pnl / 100000.0
    trade_profit = capital * pct_pnl
    capital += trade_profit
    peak = max(peak, capital)
    dd = (peak - capital) / peak * 100.0
    max_dd = max(max_dd, dd)
    yearly_comp[exit_yr]['end'] = capital
    yearly_comp[exit_yr]['trades'] += 1
    if t.net_pnl > 0:
        yearly_comp[exit_yr]['wins'] += 1

print('=== COMPOUNDED RESULTS ===')
print(f'Starting: ₹1,00,000.00 -> Ending: ₹{capital:,.2f} (+{(capital-100000)/100000*100:.1f}%)')
print(f'Multiplier: {capital/100000:.2f}x | Max DD: {max_dd:.1f}%')
for y, d in yearly_comp.items():
    gain = d['end'] - d['start']
    pct = gain / d['start'] * 100.0
    print(f'  {y}: ₹{d["start"]:,.0f} -> ₹{d["end"]:,.0f} (+₹{gain:,.0f} / +{pct:.1f}%) | {d["wins"]}/{d["trades"]} Wins')
