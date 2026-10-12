#!/usr/bin/env python3
"""
True Single-Account Portfolio Backtester for 2026 (Optimized & Exact).
"""
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares
from markets.equity.experiments.fibonacci_retracement import get_symbol_file

def load_all_candles():
    symbol_dict = {}
    for sym in NIFTY50_SYMBOLS:
        fpath = get_symbol_file(sym, 'daily')
        if not fpath:
            continue
        df = pd.read_csv(fpath)
        if df.empty or len(df) < 45:
            continue
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
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
        
        # Convert to dictionary keyed by date_str for O(1) instant lookup
        records = df.set_index('date_str').to_dict(orient='index')
        symbol_dict[sym] = (records, df)
    return symbol_dict


def run_portfolio_simulation(universe, risk_pct=5.0, start_cap=100000.0, rr_ratio=2.0):
    all_data = load_all_candles()
    valid_syms = [s for s in universe if s in all_data]
    
    # Collect all unique dates in 2026
    dates_set = set()
    for s in valid_syms:
        records, df = all_data[s]
        for d in records.keys():
            if d.startswith('2026'):
                dates_set.add(d)
    all_dates = sorted(list(dates_set))
    
    cash = start_cap
    open_positions = {}  # sym -> pos_dict
    closed_trades = []
    rejected_signals = 0
    signals_fired = 0
    
    # State tracking per symbol
    state = {}
    for sym in valid_syms:
        records, df = all_data[sym]
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

    max_val = start_cap
    min_val = start_cap
    
    # Day-by-day loop
    for current_date in all_dates:
        # Step 1: Manage open positions
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _ = all_data[sym]
            r = records.get(current_date)
            if not r:
                continue
            c_high = r['high']
            c_low = r['low']
            
            exit_p = None
            exit_r = None
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                exit_r = 'stop'
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
                    'Fees': cost['total'],
                    'Brokerage': cost['brokerage'],
                    'STT': cost['stt'],
                    'Slippage': cost['slippage'],
                    'Result': exit_r,
                    'Net (after fees)': cost['net'],
                    'Cash After': cash,
                })
                del open_positions[sym]

        # Step 2: Evaluate candidate entries
        candidate_entries = []
        for sym in valid_syms:
            records, df = all_data[sym]
            r = records.get(current_date)
            if not r:
                continue
            
            # Check swing updates
            # Find index in df
            dt_row = df[df['date_str'] == current_date]
            if not dt_row.empty:
                idx = dt_row.index[0]
                lookback = 15
                if idx >= lookback:
                    chk = idx - lookback
                    if df.loc[chk, 's_low']:
                        state[sym]['l_val'] = df.loc[chk, 'low']
                        state[sym]['l_idx'] = chk
                    if df.loc[chk, 's_high']:
                        state[sym]['h_val'] = df.loc[chk, 'high']
                        state[sym]['h_idx'] = chk
                        
            if sym in open_positions:
                continue
                
            l_val = state[sym]['l_val']
            h_val = state[sym]['h_val']
            l_idx = state[sym]['l_idx']
            h_idx = state[sym]['h_idx']
            
            if l_val and h_val and (h_idx > l_idx):
                s_range = h_val - l_val
                c_close = r['close']
                c_high = r['high']
                c_low = r['low']
                c_open = r['open']
                c_atr = r['atr'] if pd.notna(r['atr']) else c_close * 0.01
                c_ema50 = r['ema50'] if pd.notna(r['ema50']) else c_close
                
                if s_range >= 1.5 * c_atr and c_close >= c_ema50:
                    fib_top = h_val - 0.500 * s_range
                    fib_bot = h_val - 0.618 * s_range
                    fib_inv = h_val - 0.786 * s_range
                    
                    in_zone = (c_low <= fib_top) and (c_low >= fib_inv)
                    bar_rng = max(c_high - c_low, 1e-4)
                    bull_rejection = (c_close > c_open) and (c_close > fib_bot) and ((c_close - c_low) / bar_rng >= 0.40)
                    
                    if in_zone and bull_rejection:
                        entry_p = c_close
                        sl = round(l_val - 0.2 * c_atr, 2)
                        s_dist = entry_p - sl
                        if s_dist > 0:
                            tp = round(entry_p + s_dist * rr_ratio, 2)
                            candidate_entries.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'tp': tp,
                                's_dist': s_dist,
                                'date': current_date
                            })

        # Step 3: Enter positions
        for cand in candidate_entries:
            signals_fired += 1
            port_val = cash + sum(p['qty'] * p['entry_p'] for p in open_positions.values())
            target_qty = size_equity_shares(port_val, cand['entry_p'], cand['s_dist'], risk_pct, leverage=1.0)
            
            if cash >= cand['entry_p'] and target_qty > 0:
                actual_qty = min(target_qty, int(cash / cand['entry_p']))
                if actual_qty > 0:
                    req_cash = actual_qty * cand['entry_p']
                    cash -= req_cash
                    open_positions[cand['sym']] = {
                        'entry_p': cand['entry_p'],
                        'sl': cand['sl'],
                        'tp': cand['tp'],
                        'qty': actual_qty,
                        'entry_date': cand['date'],
                    }
                else:
                    rejected_signals += 1
            else:
                rejected_signals += 1

        # Track portfolio high/low
        cur_val = cash + sum(p['qty'] * all_data[s][0].get(current_date, {'close': p['entry_p']})['close'] for s, p in open_positions.items())
        max_val = max(max_val, cur_val)
        min_val = min(min_val, cur_val)

    # End of period: Mark to market
    unrealized_list = []
    unrealized_total = 0.0
    for sym, pos in open_positions.items():
        _, df = all_data[sym]
        last_p = df.iloc[-1]['close']
        unreal_pnl = (last_p - pos['entry_p']) * pos['qty']
        unrealized_total += unreal_pnl
        unrealized_list.append({
            'Stock': sym,
            'Shares': pos['qty'],
            'Entry': pos['entry_p'],
            'Current Price': last_p,
            'Unrealised': round(unreal_pnl, 1)
        })

    final_val = cash + sum(p['qty'] * all_data[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    
    return {
        'start_cap': start_cap,
        'final_val': final_val,
        'cash_remaining': cash,
        'closed_trades': closed_trades,
        'open_positions': unrealized_list,
        'unrealized_total': unrealized_total,
        'signals_fired': signals_fired,
        'rejected_signals': rejected_signals,
        'max_val': max_val,
        'min_val': min_val,
    }


if __name__ == '__main__':
    top20 = [
        'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
        'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA',
        'IOC', 'SBIN', 'ULTRACEMCO', 'SBILIFE', 'ASIANPAINT', 
        'COALINDIA', 'HEROMOTOCO', 'ONGC', 'LT', 'GRASIM'
    ]
    
    print("=== RUNNING TRUE SINGLE-ACCOUNT PORTFOLIO AUDIT (2026) ===")
    res = run_portfolio_simulation(top20)
    print(f"Starting Capital: ₹{res['start_cap']:,.2f}")
    print(f"Signals Fired: {res['signals_fired']} | Taken: {len(res['closed_trades']) + len(res['open_positions'])} | Rejected (Cash tied up): {res['rejected_signals']}")
    print(f"Peak Account Value: ₹{res['max_val']:,.2f} | Trough: ₹{res['min_val']:,.2f}")
    
    df_closed = pd.DataFrame(res['closed_trades'])
    if not df_closed.empty:
        cols = ['Stock', 'Bought', 'Sold', 'Shares', 'Result', 'Net (after fees)']
        print("\nCLOSED TRADES:")
        print(df_closed[cols].to_string(index=False))
        print(f"Closed Total: ₹{df_closed['Net (after fees)'].sum():,.2f}")
        print(f"Total Fees on Closed: ₹{df_closed['Fees'].sum():,.2f} (STT: ₹{df_closed['STT'].sum():,.2f}, Brok+DP: ₹{df_closed['Brokerage'].sum():,.2f}, Slip: ₹{df_closed['Slippage'].sum():,.2f})")
        
    df_open = pd.DataFrame(res['open_positions'])
    if not df_open.empty:
        print("\nSTILL OPEN ON 9 OCT 2026:")
        print(df_open.to_string(index=False))
        print(f"Unrealized Total: ₹{res['unrealized_total']:,.2f}")
        
    pnl = res['final_val'] - res['start_cap']
    print(f"\nFINAL ENDING VALUE (9 Oct 2026): ₹{res['final_val']:,.2f} ({pnl:+,.2f}, {pnl/res['start_cap']*100:+.2f}%)")
