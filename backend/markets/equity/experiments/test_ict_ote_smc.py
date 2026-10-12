#!/usr/bin/env python3
"""
backend/markets/equity/experiments/test_ict_ote_smc.py

Tests the authentic ICT / Neeraj Joshi SMC concepts on NIFTY 50:
1. Break of Structure (BOS / Higher High):
   - Current Swing High must break ABOVE the previous Swing High (true trend continuation).
2. Deep Discount OTE Zone:
   - Price retraces between 0.618 and 0.786 (OTE sweet spot 0.705).
3. Institutional Confirmation (Order Block / Reversal Candle):
   - Bullish Engulfing OR Strong Hammer/Pin Bar with Volume >= 20-day SMA.
   - Entry only when the high of the rejection candle is crossed (confirmation trigger).
4. Targets:
   - Target 1: Prior High (0.0 level)
   - Target 2: -0.272 Fibonacci Extension
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
        df['vol_sma20'] = ta.sma(df['volume'], length=20)
        
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

def test_smc_ote(
    start_date="2023-01-01",
    end_date="2026-10-10",
    require_bos=True,          # Require Swing High > Prior Swing High (Higher High)
    require_volume=True,       # Volume on confirmation bar > 20 SMA
    target_mode="level_0",      # "level_0" or "ext_272"
    sl_mode="below_100",       # "below_100" or "below_786"
    min_rr=1.5,
    max_pos=2,
    capital_alloc=0.50,
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
            
            c_high, c_low = r['high'], r['low']
            
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
                
        # 2. Entries
        if len(open_pos) < max_pos:
            candidates = []
            for s in symbols:
                if s in open_pos: continue
                r = RECS[s].get(d)
                if not r: continue
                
                c_close, c_open, c_high, c_low, c_vol = r['close'], r['open'], r['high'], r['low'], r.get('volume', 0)
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
                
                # Check Break of Structure (Higher High)
                if require_bos:
                    if len(valid_peaks) < 2: continue
                    prior_peak_val = float(df_s.at[valid_peaks[-2], 'high'])
                    if h_val <= prior_peak_val: continue  # Must break previous peak (BOS)
                    
                fib_618 = h_val - 0.618 * swing_size
                fib_786 = h_val - 0.786 * swing_size
                
                # Must be in OTE zone
                in_ote = (c_low <= fib_618) and (c_low >= fib_786)
                if not in_ote: continue
                
                # Reversal candle check
                bar_rng = max(c_high - c_low, 1e-4)
                body = abs(c_close - c_open)
                lower_wick = min(c_open, c_close) - c_low
                
                is_bullish = (c_close > c_open) and (c_close >= fib_618)
                is_pinbar = (lower_wick >= 1.5 * body) and (c_close >= fib_618)
                if not (is_bullish or is_pinbar): continue
                
                if require_volume:
                    v_sma = r.get('vol_sma20')
                    if pd.notna(v_sma) and c_vol < 0.9 * v_sma: continue
                    
                entry_p = c_close
                atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                
                if sl_mode == "below_100":
                    sl = round(l_val - 0.2 * atr, 2)
                else:
                    sl = round(fib_786 - 0.2 * atr, 2)
                    
                stop_dist = entry_p - sl
                if stop_dist <= 0: continue
                
                if target_mode == "level_0":
                    tp = round(h_val, 2)
                else:
                    tp = round(h_val + 0.272 * swing_size, 2)
                    
                if tp <= entry_p: continue
                
                rr = (tp - entry_p) / stop_dist
                if min_rr > 0 and rr < min_rr: continue
                
                candidates.append({
                    'symbol': s, 'entry_p': entry_p, 'sl': sl, 'tp': tp,
                    'rr': rr, 'mom': r.get('close', 0) / r.get('ema50', 1),
                })
                
            candidates.sort(key=lambda x: x['rr'], reverse=True)
            
            slots = max_pos - len(open_pos)
            for cand in candidates[:slots]:
                alloc = cash * capital_alloc
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
        ("A. Pure OTE (No BOS, No Vol, Target 0.0)", dict(require_bos=False, require_volume=False, min_rr=0.0)),
        ("B. OTE + BOS (Higher High Confirmation)", dict(require_bos=True, require_volume=False, min_rr=0.0)),
        ("C. OTE + BOS + Volume Confirmation", dict(require_bos=True, require_volume=True, min_rr=0.0)),
        ("D. OTE + BOS + Volume + Min RR >= 1.5", dict(require_bos=True, require_volume=True, min_rr=1.5)),
        ("E. OTE + BOS + Target -0.272 Extension", dict(require_bos=True, require_volume=True, target_mode="ext_272", min_rr=1.8)),
        ("F. OTE + BOS + Target -0.272 + SL 0.786", dict(require_bos=True, require_volume=True, target_mode="ext_272", sl_mode="below_786", min_rr=2.0)),
    ]
    
    for start, end, label in [("2023-01-01", "2026-10-10", "FULL PERIOD 2023 - OCT 2026"), ("2026-01-01", "2026-10-10", "YEAR 2026 ONLY")]:
        print(f"\n=======================================================")
        print(f" {label}")
        print(f"=======================================================")
        rows = []
        for name, p in grid:
            res = test_smc_ote(start_date=start, end_date=end, **p)
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
