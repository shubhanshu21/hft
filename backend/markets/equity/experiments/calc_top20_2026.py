#!/usr/bin/env python3
import sys
import pandas as pd
from markets.equity.experiments.fibonacci_retracement import run_fibonacci_backtest, get_symbol_file
from markets.equity.costs import compute_nse_equity_delivery_costs

top20 = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
    'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA',
    'IOC', 'SBIN', 'ULTRACEMCO', 'SBILIFE', 'ASIANPAINT', 
    'COALINDIA', 'HEROMOTOCO', 'ONGC', 'LT', 'GRASIM'
]

trades_2026 = []
for sym in top20:
    f = get_symbol_file(sym, 'daily')
    if not f:
        continue
    stats, trades = run_fibonacci_backtest(f, sym, 'daily', initial_capital=100000.0, risk_pct=5.0, target_type='rr2')
    for t in trades:
        exit_dt = str(t.exit_time)[:10]
        if exit_dt.startswith('2026'):
            trades_2026.append(t)

trades_2026.sort(key=lambda t: t.exit_time)

cap_compounded = 100000.0
cap_fixed = 100000.0
table_rows = []

for idx, t in enumerate(trades_2026, 1):
    cost = compute_nse_equity_delivery_costs('long', t.entry_price, t.exit_price, t.qty, symbol=t.symbol)
    
    scale = cap_compounded / 100000.0
    comp_qty = int(t.qty * scale)
    cost_comp = compute_nse_equity_delivery_costs('long', t.entry_price, t.exit_price, comp_qty, symbol=t.symbol)
    comp_net = cost_comp['net']
    cap_compounded += comp_net
    cap_fixed += t.net_pnl

    table_rows.append({
        '#': idx,
        'Stock': t.symbol,
        'Entry Date': str(t.entry_time)[:10],
        'Exit Date': str(t.exit_time)[:10],
        'Entry (₹)': round(t.entry_price, 1),
        'Exit (₹)': round(t.exit_price, 1),
        'Shares': t.qty,
        'Gross (₹)': round(t.gross_pnl, 1),
        'Brokerage': round(cost['brokerage'], 1),
        'STT': round(cost['stt'], 1),
        'Taxes+GST': round(cost['exchange_txn'] + cost['sebi'] + cost['gst'] + cost['stamp_duty'], 1),
        'Slippage': round(cost['slippage'], 1),
        'Total Fees': round(t.friction, 1),
        'Net P&L (₹)': round(t.net_pnl, 1),
        'Result': t.exit_reason,
        'Comp Bal (₹)': round(cap_compounded, 1),
    })

df = pd.DataFrame(table_rows)
print('=== TOP 20 STOCKS: 2026 ONLY ITEMIZED TRADES AUDIT ===\n')
print(df.to_string(index=False))

tot_gross = df['Gross (₹)'].sum()
tot_brok = df['Brokerage'].sum()
tot_stt = df['STT'].sum()
tot_taxes = df['Taxes+GST'].sum()
tot_slip = df['Slippage'].sum()
tot_fees = df['Total Fees'].sum()
tot_net = df['Net P&L (₹)'].sum()
wins = len(df[df['Net P&L (₹)'] > 0])
losses = len(df[df['Net P&L (₹)'] <= 0])

print('\n' + '=' * 80)
print('                  TOP 20 STOCKS - 2026 FULL AUDIT TOTALS')
print('=' * 80)
print(f'Starting Capital (Jan 1, 2026): ₹100,000.00')
print(f'Trades in 2026:                 {len(df)} ({wins} Wins / {losses} Losses — {wins/len(df)*100:.1f}% Win Rate)')
print(f'Gross Trading Profit:           ₹{tot_gross:,.2f}')
print(f'--------------------------------------------------------------------------------')
print(f'ITEMIZED STATUTORY FEES & FRICTION (DEDUCTED):')
print(f'  • Upstox Brokerage + DP:      ₹{tot_brok:,.2f}')
print(f'  • Securities STT (0.1% CNC):  ₹{tot_stt:,.2f}')
print(f'  • Exch + SEBI + Stamp + GST:  ₹{tot_taxes:,.2f}')
print(f'  • Bid-Ask Slippage:           ₹{tot_slip:,.2f}')
print(f'  TOTAL ALL FRICTION DEDUCTED:  ₹{tot_fees:,.2f}  (only {tot_fees/tot_gross*100:.1f}% fee drag)')
print(f'--------------------------------------------------------------------------------')
print(f'FINAL NET EARNINGS IN 2026:')
print(f'  • Fixed Sizing Net Profit:    ₹{tot_net:,.2f}  (+{tot_net/100000*100:.2f}% Return on ₹1 Lakh)')
print(f'  • Compounded Ending Balance:  ₹{cap_compounded:,.2f}  (+{(cap_compounded-100000)/100000*100:.2f}% Return on ₹1 Lakh)')
print('=' * 80)
