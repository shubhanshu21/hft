#!/usr/bin/env python3
"""
backend/markets/equity/experiments/test_high_quality_ote.py

Lightning-fast vectorized swing precomputation for Fibonacci OTE on Indian Equities.
Tests:
- Deep OTE Zone (0.618 - 0.786, centered at 0.705)
- Prior High (0.0 level) & -0.272 Extension Targets
- Swing Low (1.0) & 0.786 Invalidation Stop Losses
- Market Breadth Filter (% of Nifty > EMA 50)
- Minimum Risk-to-Reward Ratio Gates (1.5R to 2.5R)
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


def load_and_precompute():
    symbols_data = {}
    swing_order = 10
    
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
        
        # Precompute extrema once per stock
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        p_indices = argrelextrema(highs, np.greater_equal, order=swing_order)[0]
        t_indices = argrelextrema(lows, np.less_equal, order=swing_order)[0]
        
        df['peak_indices'] = [p_indices] * len(df)
        df['trough_indices'] = [t_indices] * len(df)
        
        symbols_data[sym] = df
        
    dates = sorted(list(set(d for s in symbols_data for d in symbols_data[s]['date_str'])))
    recs = {s: symbols_data[s].set_index('date_str').to_dict('index') for s in symbols_data}
    
    # Precompute market breadth
    breadth = {}
    for d in dates:
        above = sum(1 for s in symbols_data if recs[s].get(d) and pd.notna(recs[s][d].get('ema50')) and recs[s][d]['close'] > recs[s][d]['ema50'])
        tot = sum(1 for s in symbols_data if recs[s].get(d) and pd.notna(recs[s][d].get('ema50')))
        breadth[d] = (above / tot) if tot > 0 else 0.5
        
    return symbols_data, breadth, dates, recs

ALL_DATA, BREADTH, ALL_DATES, RECS = load_and_precompute()


def run_fast_backtest(
    start_date="2023-01-01",
    end_date="2026-10-10",
    min_breadth=0.45,
    min_swing_pct=0.06,
    min_rr_gate=1.5,
    target_mode="level_0",       # "level_0", "ext_272"
    sl_mode="below_100",         # "below_100", "below_786"
    ote_entry="deep_ote",        # "deep_ote" (0.618 - 0.786), "mid_ote" (0.50 - 0.786)
    max_pos=2,
    capital_alloc_pct=0.50,
    trail_at_halfway=False,
    trend_filter="ema200",       # "ema200", "ema50", "none"
):
    symbols = list(ALL_DATA.keys())
    test_dates = [d for d in ALL_DATES if start_date <= d <= end_date]
    date_to_idx = {s: {d: i for i, d in enumerate(ALL_DATA[s]['date_str'])} for s in symbols}
    
    cash = 100000.0
    open_pos = {}
    closed_trades = []
    
    for d in test_dates:
        # 1. Manage Exits
        for s in list(open_pos.keys()):
            pos = open_pos[s]
            r = RECS[s].get(d)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            # Check Stop Loss first
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s, 'Bought': pos['entry_date'], 'Sold': d,
                    'Entry': pos['entry_p'], 'Exit': exit_p, 'Fees': cost['total'],
                    'Net': cost['net'], 'Result': 'stop',
                })
                del open_pos[s]
                continue
                
            # Check Target
            if c_high >= pos['tp']:
                exit_p = pos['tp']
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s, 'Bought': pos['entry_date'], 'Sold': d,
                    'Entry': pos['entry_p'], 'Exit': exit_p, 'Fees': cost['total'],
                    'Net': cost['net'], 'Result': 'target',
                })
                del open_pos[s]
                continue
                
            if trail_at_halfway:
                halfway = pos['entry_p'] + 0.50 * (pos['tp'] - pos['entry_p'])
                if c_high >= halfway:
                    pos['sl'] = max(pos['sl'], pos['entry_p'])
                    
        # 2. Entries
        if len(open_pos) < max_pos:
            if min_breadth > 0 and BREADTH.get(d, 0.5) < min_breadth:
                continue
                
            candidates = []
            for s in symbols:
                if s in open_pos: continue
                r = RECS[s].get(d)
                if not r: continue
                
                c_close, c_open, c_high, c_low = r['close'], r['open'], r['high'], r['low']
                
                if trend_filter == "ema200":
                    ema200 = r.get('ema200')
                    if pd.isna(ema200) or c_close < ema200: continue
                elif trend_filter == "ema50":
                    ema50 = r.get('ema50')
                    if pd.isna(ema50) or c_close < ema50: continue
                
                idx = date_to_idx[s].get(d)
                if idx is None or idx < 30: continue
                
                # Fast lookup of extrema prior to today
                df_s = ALL_DATA[s]
                peaks = df_s.at[0, 'peak_indices']
                troughs = df_s.at[0, 'trough_indices']
                
                valid_peaks = peaks[peaks <= idx - 1]
                valid_troughs = troughs[troughs <= idx - 1]
                if len(valid_peaks) == 0 or len(valid_troughs) == 0: continue
                
                p_idx = valid_peaks[-1]
                t_idx = valid_troughs[-1]
                if p_idx <= t_idx: continue  # Must be Low -> High
                
                h_val = float(df_s.at[p_idx, 'high'])
                l_val = float(df_s.at[t_idx, 'low'])
                swing_size = h_val - l_val
                if swing_size / l_val < min_swing_pct: continue
                
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                fib_786 = h_val - 0.786 * swing_size
                
                if ote_entry == "deep_ote":
                    in_zone = (c_low <= fib_618) and (c_low >= fib_786)
                else:
                    in_zone = (c_low <= fib_500) and (c_low >= fib_786)
                    
                if not in_zone: continue
                
                # Rejection candle
                bar_rng = max(c_high - c_low, 1e-4)
                lower_wick = min(c_open, c_close) - c_low
                is_rejection = (lower_wick / bar_rng >= 0.25) or (c_close > c_open)
                if not is_rejection or c_close < fib_786: continue
                
                entry_p = c_close
                atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                
                if sl_mode == "below_786":
                    sl = round(fib_786 - 0.2 * atr, 2)
                else:
                    sl = round(l_val - 0.2 * atr, 2)
                    
                stop_dist = entry_p - sl
                if stop_dist <= 0: continue
                
                if target_mode == "level_0":
                    tp = round(h_val, 2)
                else:
                    tp = round(h_val + 0.272 * swing_size, 2)
                    
                if tp <= entry_p: continue
                
                rr = (tp - entry_p) / stop_dist
                if min_rr_gate > 0 and rr < min_rr_gate: continue
                
                candidates.append({
                    'symbol': s, 'entry_p': entry_p, 'sl': sl, 'tp': tp,
                    'rr': rr, 'mom': r.get('mom60', 0.0),
                })
                
            candidates.sort(key=lambda x: x['rr'], reverse=True)
            
            slots = max_pos - len(open_pos)
            for cand in candidates[:slots]:
                alloc = cash * capital_alloc_pct
                qty = int(alloc // cand['entry_p'])
                if qty <= 0: continue
                
                cost = compute_nse_equity_delivery_costs('long', cand['entry_p'], cand['entry_p'], qty, symbol=cand['symbol'])
                if (qty * cand['entry_p']) + cost['total'] > cash: continue
                
                cash -= (qty * cand['entry_p']) + cost['total']
                open_pos[cand['symbol']] = {
                    'entry_date': d, 'entry_p': cand['entry_p'], 'qty': qty,
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
    grid = [
        ("1. OTE Baseline: Breadth=0, Target=0.0, SL=1.0", dict(min_breadth=0.0, min_rr_gate=0.0, target_mode="level_0", sl_mode="below_100")),
        ("2. OTE + Breadth >= 50% + Target=0.0", dict(min_breadth=0.50, min_rr_gate=0.0, target_mode="level_0", sl_mode="below_100")),
        ("3. OTE + Breadth >= 50% + RR >= 1.5 + Target=0.0", dict(min_breadth=0.50, min_rr_gate=1.5, target_mode="level_0", sl_mode="below_100")),
        ("4. OTE + Breadth >= 50% + RR >= 1.8 + Target=0.0", dict(min_breadth=0.50, min_rr_gate=1.8, target_mode="level_0", sl_mode="below_100")),
        ("5. OTE + Breadth >= 45% + RR >= 2.0 + Target=-0.272 Ext", dict(min_breadth=0.45, min_rr_gate=2.0, target_mode="ext_272", sl_mode="below_100")),
        ("6. OTE + Breadth >= 50% + RR >= 2.0 + Target=-0.272 Ext", dict(min_breadth=0.50, min_rr_gate=2.0, target_mode="ext_272", sl_mode="below_100")),
        ("7. OTE + Breadth >= 45% + RR >= 1.5 + SL below 0.786", dict(min_breadth=0.45, min_rr_gate=1.5, target_mode="level_0", sl_mode="below_786")),
        ("8. OTE + Breadth >= 50% + RR >= 2.0 + Trail at 50% TP", dict(min_breadth=0.50, min_rr_gate=2.0, target_mode="ext_272", sl_mode="below_100", trail_at_halfway=True)),
        ("9. OTE 1 Concentrated Pos (100% Cash) | Breadth >= 50% | RR >= 1.8", dict(min_breadth=0.50, min_rr_gate=1.8, target_mode="level_0", sl_mode="below_100", max_pos=1, capital_alloc_pct=1.00)),
        ("10. OTE 1 Concentrated Pos | Breadth >= 50% | Target=-0.272 | RR >= 2.0", dict(min_breadth=0.50, min_rr_gate=2.0, target_mode="ext_272", sl_mode="below_100", max_pos=1, capital_alloc_pct=1.00)),
    ]
    
    for start, end, label in [("2023-01-01", "2026-10-10", "FULL PERIOD (DEC 2022 - OCT 2026)"), ("2026-01-01", "2026-10-10", "YEAR 2026 CHOP AUDIT")]:
        print(f"\n=======================================================")
        print(f" {label}")
        print(f"=======================================================")
        rows = []
        for name, p in grid:
            res = run_fast_backtest(start_date=start, end_date=end, **p)
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
