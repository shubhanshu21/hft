#!/usr/bin/env python3
"""
backend/markets/equity/experiments/find_profitable_fib_edge.py

Systematically tests across all Fibonacci parameters on daily equity:
- Universe: Nifty 50 vs Top Relative Strength Momentum Leaders
- Retracement entry levels: 0.382, 0.500, 0.618, 0.705
- Target levels: Prior High (0.0), -0.272 Ext, -0.618 Ext, 2R
- Stop loss: 0.618, 0.786, 1.0 (Swing Low)
- Holding period / Time stop: 5 days, 10 days, 15 days, No limit
- Single ₹100,000 cash account, real NSE delivery statutory fees
"""

import os
import sys
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

sys.path.insert(0, '/var/www/html/hft/backend')
from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"


def load_universe():
    symbols_data = {}
    swing_order = 8
    
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_daily.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        if len(df) < 120: continue
        
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['mom60'] = df['close'].pct_change(60)
        df['mom120'] = df['close'].pct_change(120)
        
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        p_indices = argrelextrema(highs, np.greater_equal, order=swing_order)[0]
        t_indices = argrelextrema(lows, np.less_equal, order=swing_order)[0]
        
        df['peak_indices'] = [p_indices] * len(df)
        df['trough_indices'] = [t_indices] * len(df)
        
        symbols_data[sym] = df
        
    dates = sorted(list(set(d for s in symbols_data for d in symbols_data[s]['date_str'])))
    recs = {s: symbols_data[s].set_index('date_str').to_dict('index') for s in symbols_data}
    
    return symbols_data, dates, recs

ALL_DATA, ALL_DATES, RECS = load_universe()


def backtest(
    start_date="2023-01-01",
    end_date="2026-10-10",
    fib_entry_min=0.50,         # e.g. 0.50
    fib_entry_max=0.618,        # e.g. 0.618 (or 0.786 for OTE)
    sl_level=1.0,               # 0.786 or 1.0 (Swing Low)
    tp_mode="level_0",          # "level_0", "ext_272", "rr2"
    use_top_leaders=True,       # Filter to top 15 leaders by 6-month momentum
    max_hold_days=15,           # Time stop in trading days (prevents dead money)
    max_pos=2,
    capital_alloc=0.50,
):
    symbols = list(ALL_DATA.keys())
    test_dates = [d for d in ALL_DATES if start_date <= d <= end_date]
    date_to_idx = {s: {d: i for i, d in enumerate(ALL_DATA[s]['date_str'])} for s in symbols}
    
    cash = 100000.0
    open_pos = {}
    closed_trades = []
    
    for d_idx, d in enumerate(test_dates):
        # 1. Manage Exits
        for s in list(open_pos.keys()):
            pos = open_pos[s]
            r = RECS[s].get(d)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            days_held = d_idx - pos['entry_d_idx']
            
            # Check Stop
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s, 'Net': cost['net'], 'Fees': cost['total'], 'Result': 'stop',
                })
                del open_pos[s]
                continue
                
            # Check Target
            if c_high >= pos['tp']:
                exit_p = pos['tp']
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s, 'Net': cost['net'], 'Fees': cost['total'], 'Result': 'target',
                })
                del open_pos[s]
                continue
                
            # Check Time Stop (Dead money exit)
            if max_hold_days > 0 and days_held >= max_hold_days:
                exit_p = c_close
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s, 'Net': cost['net'], 'Fees': cost['total'], 'Result': 'time_stop',
                })
                del open_pos[s]
                continue
                
        # 2. Entries
        if len(open_pos) < max_pos:
            eligible_symbols = symbols
            if use_top_leaders:
                moms = []
                for s in symbols:
                    r = RECS[s].get(d)
                    if r and pd.notna(r.get('mom120')) and pd.notna(r.get('ema200')):
                        if r['close'] > r['ema200']:
                            moms.append((s, r['mom120']))
                moms.sort(key=lambda x: x[1], reverse=True)
                eligible_symbols = [s for s, m in moms[:12]]
                
            candidates = []
            for s in eligible_symbols:
                if s in open_pos: continue
                r = RECS[s].get(d)
                if not r: continue
                
                c_close, c_open, c_high, c_low = r['close'], r['open'], r['high'], r['low']
                ema50 = r.get('ema50')
                if pd.isna(ema50) or c_close < ema50: continue
                
                idx = date_to_idx[s].get(d)
                if idx is None or idx < 30: continue
                
                df_s = ALL_DATA[s]
                peaks = df_s.at[0, 'peak_indices']
                troughs = df_s.at[0, 'trough_indices']
                
                valid_peaks = peaks[peaks <= idx - 1]
                valid_troughs = troughs[troughs <= idx - 1]
                if len(valid_peaks) < 1 or len(valid_troughs) < 1: continue
                
                p_idx = valid_peaks[-1]
                t_idx = valid_troughs[-1]
                if p_idx <= t_idx: continue  # Upward leg
                
                h_val = float(df_s.at[p_idx, 'high'])
                l_val = float(df_s.at[t_idx, 'low'])
                swing_size = h_val - l_val
                if swing_size / l_val < 0.05: continue
                
                fib_min = h_val - fib_entry_min * swing_size
                fib_max = h_val - fib_entry_max * swing_size
                
                # In retracement zone
                in_zone = (c_low <= fib_min) and (c_low >= fib_max)
                if not in_zone: continue
                
                # Rejection
                if c_close < c_open: continue
                if c_close < fib_max: continue
                
                entry_p = c_close
                atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                
                if sl_level == 1.0:
                    sl = round(l_val - 0.2 * atr, 2)
                elif sl_level == 0.786:
                    sl = round((h_val - 0.786 * swing_size) - 0.2 * atr, 2)
                elif sl_level == 0.618:
                    sl = round((h_val - 0.618 * swing_size) - 0.2 * atr, 2)
                    
                stop_dist = entry_p - sl
                if stop_dist <= 0: continue
                
                if tp_mode == "level_0":
                    tp = round(h_val, 2)
                elif tp_mode == "ext_272":
                    tp = round(h_val + 0.272 * swing_size, 2)
                elif tp_mode == "rr2":
                    tp = round(entry_p + 2.0 * stop_dist, 2)
                    
                if tp <= entry_p: continue
                
                rr = (tp - entry_p) / stop_dist
                candidates.append({
                    'symbol': s, 'entry_p': entry_p, 'sl': sl, 'tp': tp,
                    'rr': rr, 'mom': r.get('mom120', 0),
                })
                
            candidates.sort(key=lambda x: x['mom'], reverse=True)
            
            slots = max_pos - len(open_pos)
            for cand in candidates[:slots]:
                alloc = cash * capital_alloc
                qty = int(alloc // cand['entry_p'])
                if qty <= 0: continue
                
                cost = compute_nse_equity_delivery_costs('long', cand['entry_p'], cand['entry_p'], qty, symbol=cand['symbol'])
                if (qty * cand['entry_p']) + cost['total'] > cash: continue
                
                cash -= (qty * cand['entry_p']) + cost['total']
                open_pos[cand['symbol']] = {
                    'entry_d_idx': d_idx, 'entry_p': cand['entry_p'], 'qty': qty,
                    'sl': cand['sl'], 'tp': cand['tp'],
                }
                
    final_eq = cash
    for s, pos in open_pos.items():
        r = RECS[s].get(test_dates[-1])
        lp = r['close'] if r else pos['entry_p']
        cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], lp, pos['qty'], symbol=s)
        final_eq += (pos['qty'] * lp) - cost['total']
        
    trades = len(closed_trades)
    wins = [t for t in closed_trades if t['Net'] > 0]
    wr = (len(wins) / trades * 100) if trades > 0 else 0.0
    net_pnl = sum(t['Net'] for t in closed_trades)
    fees = sum(t['Fees'] for t in closed_trades)
    
    return {
        'trades': trades,
        'wr': round(wr, 1),
        'net': round(net_pnl, 1),
        'fees': round(fees, 1),
        'equity': round(final_eq, 1),
        'roi': round(((final_eq - 100000.0) / 1000.0), 2),
        'closed_trades': closed_trades,
    }


if __name__ == '__main__':
    experiments = [
        # Shallow Pullbacks (Trend Continuation)
        ("1. Shallow 0.382-0.500 Pullback | Top Leaders | Target 0.0 | Max Hold 15d", dict(fib_entry_min=0.382, fib_entry_max=0.500, sl_level=0.786, tp_mode="level_0", use_top_leaders=True, max_hold_days=15)),
        ("2. Shallow 0.382-0.500 Pullback | Top Leaders | Target -0.272 Ext | Max Hold 20d", dict(fib_entry_min=0.382, fib_entry_max=0.500, sl_level=0.786, tp_mode="ext_272", use_top_leaders=True, max_hold_days=20)),
        # Golden Pocket 0.50 - 0.618
        ("3. Golden Pocket 0.50-0.618 | Top Leaders | Target 0.0 | Max Hold 15d", dict(fib_entry_min=0.500, fib_entry_max=0.618, sl_level=1.0, tp_mode="level_0", use_top_leaders=True, max_hold_days=15)),
        ("4. Golden Pocket 0.50-0.618 | Top Leaders | Target -0.272 Ext | Max Hold 20d", dict(fib_entry_min=0.500, fib_entry_max=0.618, sl_level=1.0, tp_mode="ext_272", use_top_leaders=True, max_hold_days=20)),
        ("5. Golden Pocket 0.50-0.618 | Top Leaders | Target 0.0 | No Time Stop", dict(fib_entry_min=0.500, fib_entry_max=0.618, sl_level=1.0, tp_mode="level_0", use_top_leaders=True, max_hold_days=0)),
        # Deep OTE 0.618 - 0.786
        ("6. Neeraj OTE (0.618-0.786) | Top Leaders | Target 0.0 | Max Hold 15d", dict(fib_entry_min=0.618, fib_entry_max=0.786, sl_level=1.0, tp_mode="level_0", use_top_leaders=True, max_hold_days=15)),
        ("7. Neeraj OTE (0.618-0.786) | Top Leaders | Target -0.272 Ext | Max Hold 20d", dict(fib_entry_min=0.618, fib_entry_max=0.786, sl_level=1.0, tp_mode="ext_272", use_top_leaders=True, max_hold_days=20)),
        ("8. Neeraj OTE (0.618-0.786) | Top Leaders | Target 2.0R | Max Hold 20d", dict(fib_entry_min=0.618, fib_entry_max=0.786, sl_level=1.0, tp_mode="rr2", use_top_leaders=True, max_hold_days=20)),
    ]
    
    for start, end, period in [("2023-01-01", "2026-10-10", "FULL PERIOD (2023 - OCT 2026)"), ("2026-01-01", "2026-10-10", "YEAR 2026 CHOP AUDIT")]:
        print(f"\n=======================================================")
        print(f" {period}")
        print(f"=======================================================")
        rows = []
        for name, p in experiments:
            res = backtest(start_date=start, end_date=end, **p)
            rows.append({
                'Setup': name,
                'Trades': res['trades'],
                'Win Rate': f"{res['wr']}%",
                'Fees (₹)': f"₹{res['fees']:,.0f}",
                'Net P&L (₹)': f"₹{res['net']:,.0f}",
                'Ending Equity': f"₹{res['equity']:,.0f}",
                'ROI (%)': f"{res['roi']:+.2f}%",
            })
        print(pd.DataFrame(rows).to_string(index=False))
