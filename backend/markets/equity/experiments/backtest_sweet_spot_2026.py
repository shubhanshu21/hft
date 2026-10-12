#!/usr/bin/env python3
"""
Full Itemized 2026 Backtest of the Fibonacci Sweet Spot Strategy.
- Starting Capital: ₹100,000.00 (Single Shared Account)
- Max 3 active positions (up to 33% capital per position)
- Impulse wave >= 6% expansion
- Golden Pocket 50%–61.8% discount + Rejection Candle
- Target: Prior Swing High
- Protection: Move SL to Breakeven at +1R
- Deducts all Upstox brokerage, STT 0.1%, DP charges, GST, exchange fees, and slippage.
"""
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs
from markets.equity.experiments.fibonacci_retracement import get_symbol_file

def load_data():
    symbol_dict = {}
    for sym in NIFTY50_SYMBOLS:
        f_d = get_symbol_file(sym, 'daily')
        if not f_d: continue
        df = pd.read_csv(f_d).sort_values('timestamp').reset_index(drop=True)
        if len(df) < 50: continue
        
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        
        highs = df['high'].to_numpy(dtype=np.float64)
        lows = df['low'].to_numpy(dtype=np.float64)
        n = len(df)
        lookback = 15
        s_high = np.zeros(n, dtype=bool)
        s_low = np.zeros(n, dtype=bool)
        for idx in range(lookback, n - lookback):
            if highs[idx] == np.max(highs[idx - lookback : idx + lookback + 1]):
                s_high[idx] = True
            if lows[idx] == np.min(lows[idx - lookback : idx + lookback + 1]):
                s_low[idx] = True
        df['s_high'] = s_high
        df['s_low'] = s_low
        
        records = df.set_index('date_str').to_dict(orient='index')
        date_to_idx = {d: i for i, d in enumerate(df['date_str'])}
        symbol_dict[sym] = (records, df, date_to_idx)
    return symbol_dict

ALL_DATA = load_data()


def run_itemized_audit(
    min_swing_pct=0.06,
    fib_shallow=0.500,
    fib_deep=0.618,
    max_active_pos=3,
    max_alloc_pct=0.33,
    trail_be_at_1r=True,
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in ALL_DATA]
    
    dates_set = set()
    for s in symbols:
        records, _, _ = ALL_DATA[s]
        for d in records.keys():
            if d.startswith('2026'):
                dates_set.add(d)
    all_dates = sorted(list(dates_set))
    
    cash = 100000.0
    open_positions = {}
    closed_trades = []
    
    # State tracking
    state = {}
    for sym in symbols:
        records, df, _ = ALL_DATA[sym]
        lookback = 15
        df_pre = df[df['date_str'] < '2026-01-01']
        last_l_val, last_l_idx = None, None
        last_h_val, last_h_idx = None, None
        for i in range(lookback * 2, len(df_pre)):
            chk = i - lookback
            if df.loc[chk, 's_low']:
                last_l_val = df.loc[chk, 'low']
                last_l_idx = chk
            if df.loc[chk, 's_high']:
                last_h_val = df.loc[chk, 'high']
                last_h_idx = chk
        state[sym] = {'l_val': last_l_val, 'l_idx': last_l_idx, 'h_val': last_h_val, 'h_idx': last_h_idx}

    for current_date in all_dates:
        # 1. Manage Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            c_high = r['high']
            c_low = r['low']
            
            if trail_be_at_1r:
                risk_amt = pos['entry_p'] - pos['orig_sl']
                if c_high >= pos['entry_p'] + risk_amt:
                    if pos['sl'] < pos['entry_p']:
                        pos['sl'] = pos['entry_p']
                        
            exit_p = None
            exit_r = None
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                exit_r = 'stop' if pos['sl'] < pos['entry_p'] else 'breakeven'
            elif c_high >= pos['tp']:
                exit_p = pos['tp']
                exit_r = 'target'
                
            if exit_p is not None:
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=sym)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': sym,
                    'Bought': pos['entry_date'],
                    'Sold': current_date,
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': cost['gross'],
                    'Brokerage': cost['brokerage'],
                    'STT': cost['stt'],
                    'Taxes': cost['exchange_txn'] + cost['sebi'] + cost['gst'] + cost['stamp_duty'],
                    'Slippage': cost['slippage'],
                    'Total Fees': cost['total'],
                    'Net P&L': cost['net'],
                    'Result': exit_r,
                    'Cash After': cash,
                })
                del open_positions[sym]

        # 2. Entries
        candidates = []
        for sym in symbols:
            records, df, date_to_idx = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
            idx = date_to_idx.get(current_date)
            if idx is not None:
                lookback = 15
                if idx >= lookback:
                    chk = idx - lookback
                    if df.loc[chk, 's_low']:
                        state[sym]['l_val'] = df.loc[chk, 'low']
                        state[sym]['l_idx'] = chk
                    if df.loc[chk, 's_high']:
                        state[sym]['h_val'] = df.loc[chk, 'high']
                        state[sym]['h_idx'] = chk
                        
            if sym in open_positions: continue
            
            l_val = state[sym]['l_val']
            h_val = state[sym]['h_val']
            l_idx = state[sym]['l_idx']
            h_idx = state[sym]['h_idx']
            
            if l_val and h_val and (h_idx > l_idx):
                s_range = h_val - l_val
                impulse_pct = s_range / l_val
                
                c_close = r['close']
                c_high = r['high']
                c_low = r['low']
                c_open = r['open']
                c_atr = r['atr'] if pd.notna(r['atr']) else c_close * 0.01
                c_ema50 = r['ema50'] if pd.notna(r['ema50']) else c_close
                
                if impulse_pct >= min_swing_pct and c_close >= c_ema50:
                    fib_top = h_val - fib_shallow * s_range
                    fib_bot = h_val - fib_deep * s_range
                    fib_inv = h_val - 0.786 * s_range
                    
                    in_zone = (c_low <= fib_top) and (c_low >= fib_inv)
                    bar_rng = max(c_high - c_low, 1e-4)
                    bull_rejection = (c_close > c_open) and (c_close > fib_bot) and ((c_close - c_low) / bar_rng >= 0.40)
                    
                    if in_zone and bull_rejection:
                        entry_p = c_close
                        sl = round(l_val - 0.2 * c_atr, 2)
                        s_dist = entry_p - sl
                        if s_dist > 0:
                            tp = round(h_val, 2)  # Prior swing high
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'orig_sl': sl,
                                'tp': tp,
                                's_dist': s_dist,
                            })

        # Sizing & Allocation
        port_equity = cash + sum(p['qty'] * p['entry_p'] for p in open_positions.values())
        max_alloc = port_equity * max_alloc_pct
        for cand in candidates:
            if len(open_positions) >= max_active_pos: break
            qty = min(int(max_alloc / cand['entry_p']), int(cash / cand['entry_p']))
            if qty > 0:
                cash -= qty * cand['entry_p']
                open_positions[cand['sym']] = {
                    'entry_p': cand['entry_p'],
                    'sl': cand['sl'],
                    'orig_sl': cand['orig_sl'],
                    'tp': cand['tp'],
                    'qty': qty,
                    'entry_date': current_date,
                }

    # Mark to market at end (9 Oct 2026)
    unreal_list = []
    unreal_total = 0.0
    for sym, pos in open_positions.items():
        _, df, _ = ALL_DATA[sym]
        last_p = df.iloc[-1]['close']
        unreal_pnl = (last_p - pos['entry_p']) * pos['qty']
        unreal_total += unreal_pnl
        unreal_list.append({
            'Stock': sym,
            'Shares': pos['qty'],
            'Entry (₹)': pos['entry_p'],
            'Current Price (₹)': last_p,
            'Unrealised (₹)': round(unreal_pnl, 1)
        })

    final_port_equity = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    
    return closed_trades, unreal_list, cash, final_port_equity


if __name__ == '__main__':
    closed, open_pos, cash_rem, final_equity = run_itemized_audit()
    df_closed = pd.DataFrame(closed)
    
    print('=== FIBONACCI SWEET SPOT: 2026 AUDIT (₹100,000 STARTING CAPITAL) ===\n')
    if not df_closed.empty:
        df_closed.index = range(1, len(df_closed) + 1)
        cols = ['Stock', 'Bought', 'Sold', 'Shares', 'Entry', 'Exit', 'Gross', 'Brokerage', 'STT', 'Slippage', 'Total Fees', 'Net P&L', 'Result']
        print(df_closed[cols].to_string())
        
        tot_gross = df_closed['Gross'].sum()
        tot_fees = df_closed['Total Fees'].sum()
        tot_net = df_closed['Net P&L'].sum()
        wins = len(df_closed[df_closed['Net P&L'] > 0])
        be = len(df_closed[df_closed['Result'] == 'breakeven'])
        losses = len(df_closed[(df_closed['Net P&L'] <= 0) & (df_closed['Result'] != 'breakeven')])
        
        print('\n' + '=' * 80)
        print('                          CLOSED TRADES SUMMARY')
        print('=' * 80)
        print(f"Total Closed Trades: {len(df_closed)} ({wins} Wins / {be} Breakeven / {losses} Losses — {wins/len(df_closed)*100:.1f}% Win Rate)")
        print(f"Gross Profit:        ₹{tot_gross:,.2f}")
        print(f"Total Fees Deducted: ₹{tot_fees:,.2f} (STT: ₹{df_closed['STT'].sum():,.2f}, Brok+DP: ₹{df_closed['Brokerage'].sum():,.2f}, Slippage: ₹{df_closed['Slippage'].sum():,.2f})")
        print(f"Net Closed P&L:      ₹{tot_net:,.2f}")
        
    df_open = pd.DataFrame(open_pos)
    if not df_open.empty:
        print('\n' + '=' * 80)
        print('                    STILL OPEN ON 9 OCT 2026 (UNREALIZED)')
        print('=' * 80)
        print(df_open.to_string(index=False))
        unreal_sum = df_open['Unrealised (₹)'].sum()
        print(f"Total Unrealized P&L: ₹{unreal_sum:,.2f}")
    else:
        unreal_sum = 0.0
        
    pnl = final_equity - 100000.0
    print('\n' + '=' * 80)
    print('                      FINAL PORTFOLIO ACCOUNT TOTALS')
    print('=' * 80)
    print(f"Starting Capital (1 Jan 2026): ₹100,000.00")
    print(f"Ending Equity (9 Oct 2026):    ₹{final_equity:,.2f} ({pnl:+,.2f}, {pnl/1000:+.2f}%)")
    print(f"Cash Remaining:                ₹{cash_rem:,.2f}")
    print('=' * 80)
