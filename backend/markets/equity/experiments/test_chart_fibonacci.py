#!/usr/bin/env python3
"""
Simulate EXACT Fibonacci Retracement Setup from user's chart:
1. Impulse leg: Swing Low (1.0) to Swing High (0.0).
2. Retracement: Enters the Golden Pocket (0.50 to 0.618).
3. Invalidation: Price must not break below 0.786 (or 1.0).
4. Target: Retest of Prior Swing High (0.0 level).
5. Stop Loss: Placed just below 0.618 or 0.786.
6. Portfolio: True single-account ₹100,000 cash balance, max 2 positions, full Upstox statutory fees.
"""
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"

def load_data():
    symbols_data = {}
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_daily.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        if len(df) < 80: continue
        
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['mom60'] = df['close'].pct_change(60)
        
        symbols_data[sym] = df
    return symbols_data

ALL_DATA = load_data()

def test_chart_fibonacci(
    target_mode='level_0',       # 'level_0' (0.0 Prior High), 'level_ext' (-0.272 extension), 'rr2'
    sl_mode='below_786',         # 'below_618', 'below_786', 'below_100'
    trail_mode='none',           # 'none', 'trail_at_half_target', 'trail_at_1r'
    min_impulse_pct=0.05,        # 5% impulse leg minimum
    max_active_pos=2,
    capital_alloc_pct=0.50,
    use_rs_filter=True,          # Require macro uptrend Close > EMA 50
    start_year='2026',
):
    symbols = list(ALL_DATA.keys())
    dates = sorted(list(set(d for s in symbols for d in ALL_DATA[s][ALL_DATA[s]['date_str'] >= f"{start_year}-01-01"]['date_str'])))
    
    cash = 100000.0
    open_pos = {}
    closed_trades = []
    
    recs = {s: ALL_DATA[s].set_index('date_str').to_dict('index') for s in symbols}
    date_to_idx = {s: {d: i for i, d in enumerate(ALL_DATA[s]['date_str'])} for s in symbols}
    
    lookback = 12
    
    for d in dates:
        # 1. Manage Exits
        for s in list(open_pos.keys()):
            pos = open_pos[s]
            r = recs[s].get(d)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            exit_p = None
            exit_r = None
            
            # Check Stop Loss First (checked on existing stop before today)
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                exit_r = 'stop' if pos['sl'] < pos['entry_p'] else 'breakeven'
            elif c_high >= pos['tp']:
                exit_p = pos['tp']
                exit_r = 'target'
                
            if exit_p is not None:
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': s,
                    'Bought': pos['entry_date'],
                    'Sold': d,
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': cost['gross'],
                    'Fees': cost['total'],
                    'Net': cost['net'],
                    'Result': exit_r,
                })
                del open_pos[s]
            else:
                # Update trailing stop for subsequent bars
                if trail_mode == 'trail_at_half_target':
                    half_tp = pos['entry_p'] + 0.5 * (pos['tp'] - pos['entry_p'])
                    if c_high >= half_tp:
                        pos['sl'] = max(pos['sl'], pos['entry_p'])
                elif trail_mode == 'trail_at_1r':
                    risk_unit = pos['entry_p'] - pos['orig_sl']
                    if c_high >= (pos['entry_p'] + risk_unit):
                        pos['sl'] = max(pos['sl'], pos['entry_p'])

        # 2. Candidate Screening
        if len(open_pos) < max_active_pos:
            candidates = []
            
            # Rank by Relative Strength if filter enabled
            eligible_symbols = symbols
            if use_rs_filter:
                moms = []
                for s in symbols:
                    r = recs[s].get(d)
                    if r and pd.notna(r.get('mom60')) and pd.notna(r.get('ema50')):
                        if r['close'] > r['ema50']:
                            moms.append((s, r['mom60']))
                moms.sort(key=lambda x: x[1], reverse=True)
                eligible_symbols = [s for s, m in moms[:10]]
                
            for sym in eligible_symbols:
                if sym in open_pos: continue
                r = recs[sym].get(d)
                if not r: continue
                
                c_close, c_open, c_high, c_low = r['close'], r['open'], r['high'], r['low']
                ema50 = r.get('ema50')
                if pd.isna(ema50) or c_close < ema50: continue
                
                idx = date_to_idx[sym].get(d)
                if idx is None or idx < lookback * 3: continue
                
                df_sym = ALL_DATA[sym]
                # Look for swing points up to yesterday (no lookahead)
                sub_highs = df_sym.loc[: idx - 1, 'high'].to_numpy()
                sub_lows = df_sym.loc[: idx - 1, 'low'].to_numpy()
                
                peaks = argrelextrema(sub_highs, np.greater_equal, order=lookback)[0]
                troughs = argrelextrema(sub_lows, np.less_equal, order=lookback)[0]
                if len(peaks) == 0 or len(troughs) == 0: continue
                
                p_idx = peaks[-1]
                t_idx = troughs[-1]
                if p_idx <= t_idx: continue  # Must be an upward impulse (Low followed by High)
                
                h_val = float(sub_highs[p_idx])
                l_val = float(sub_lows[t_idx])
                swing_size = h_val - l_val
                if swing_size / l_val < min_impulse_pct: continue
                
                # Canonical Fibonacci Retracement Levels:
                # 0.0 = Prior High (h_val)
                # 0.5 = h_val - 0.500 * swing_size
                # 0.618 = h_val - 0.618 * swing_size
                # 0.786 = h_val - 0.786 * swing_size
                # 1.0 = Swing Low (l_val)
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                fib_786 = h_val - 0.786 * swing_size
                
                # Check if price pulled back into Golden Pocket (0.50 to 0.618) without violating invalidation
                in_golden_pocket = (c_low <= fib_500) and (c_low >= fib_786)
                if in_golden_pocket:
                    # Bullish rejection: Green candle (close > open) or lower wick rejection, holding above 0.618
                    lower_wick = min(c_open, c_close) - c_low
                    bar_rng = max(c_high - c_low, 1e-4)
                    bullish_rejection = ((lower_wick / bar_rng >= 0.25) or (c_close > c_open)) and (c_close >= fib_618)
                    
                    if bullish_rejection:
                        entry_p = c_close
                        atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                        
                        # Stop Loss Modes
                        if sl_mode == 'below_618':
                            sl = round(fib_618 - 0.3 * atr, 2)
                        elif sl_mode == 'below_786':
                            sl = round(fib_786 - 0.2 * atr, 2)
                        elif sl_mode == 'below_100':
                            sl = round(l_val - 0.2 * atr, 2)
                        else:
                            sl = round(fib_786 - 0.2 * atr, 2)
                            
                        stop_dist = entry_p - sl
                        if stop_dist <= 0: continue
                        
                        # Target Modes
                        if target_mode == 'level_0':
                            tp = round(h_val, 2)  # Retest of Prior Swing High (0.0 level)
                        elif target_mode == 'level_ext':
                            tp = round(h_val + 0.272 * swing_size, 2)  # -0.272 extension target
                        elif target_mode == 'rr2':
                            tp = round(entry_p + 2.0 * stop_dist, 2)
                        else:
                            tp = round(h_val, 2)
                            
                        if tp > entry_p and (tp - entry_p) / stop_dist >= 1.0:
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'tp': tp,
                                'stop_dist': stop_dist,
                            })

            # 3. Position Allocation on Single Account Cash Pool
            port_eq = cash + sum(p['qty'] * recs[s].get(d, {}).get('close', p['entry_p']) for s, p in open_pos.items())
            alloc_per_pos = port_eq * capital_alloc_pct
            for cand in candidates:
                if len(open_pos) >= max_active_pos: break
                qty = min(int(alloc_per_pos / cand['entry_p']), int(cash / cand['entry_p']))
                if qty > 0:
                    cost_est = qty * cand['entry_p']
                    if cash >= cost_est:
                        cash -= cost_est
                        open_pos[cand['sym']] = {
                            'entry_p': cand['entry_p'],
                            'sl': cand['sl'],
                            'orig_sl': cand['sl'],
                            'tp': cand['tp'],
                            'qty': qty,
                            'entry_date': d,
                        }

    # Totals
    final_eq = cash + sum(p['qty'] * ALL_DATA[s].iloc[-1]['close'] for s, p in open_pos.items())
    wins = [t for t in closed_trades if t['Net'] > 0]
    wr = round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0
    return {
        'trades': len(closed_trades),
        'wins': len(wins),
        'wr': wr,
        'gross': round(sum(t['Gross'] for t in closed_trades), 1),
        'fees': round(sum(t['Fees'] for t in closed_trades), 1),
        'net': round(sum(t['Net'] for t in closed_trades), 1),
        'equity': round(final_eq, 1),
        'roi': round((final_eq - 100000.0) / 1000.0, 2),
        'closed_trades': closed_trades,
    }

if __name__ == '__main__':
    grid = [
        ("A. Target = 0.0 Level (Prior High) | SL = Below 0.786 | NO Trail (Full Room)", dict(target_mode='level_0', sl_mode='below_786', trail_mode='none')),
        ("B. Target = 0.0 Level (Prior High) | SL = Below 0.786 | Trail at 50% Target", dict(target_mode='level_0', sl_mode='below_786', trail_mode='trail_at_half_target')),
        ("C. Target = 0.0 Level (Prior High) | SL = Below 0.618 (Tight) | NO Trail", dict(target_mode='level_0', sl_mode='below_618', trail_mode='none')),
        ("D. Target = 0.0 Level (Prior High) | SL = Below 1.0 (Swing Low) | NO Trail", dict(target_mode='level_0', sl_mode='below_100', trail_mode='none')),
        ("E. Target = -0.272 Extension | SL = Below 0.786 | Trail at 50% Target", dict(target_mode='level_ext', sl_mode='below_786', trail_mode='trail_at_half_target')),
        ("F. Target = 2.0:1 RR | SL = Below 0.786 | Trail at 1R", dict(target_mode='rr2', sl_mode='below_786', trail_mode='trail_at_1r')),
        ("G. Target = 0.0 Level | Top 5 Leaders | 1 Concentrated Position (100% Cash)", dict(target_mode='level_0', sl_mode='below_786', trail_mode='none', max_active_pos=1, capital_alloc_pct=1.00)),
    ]
    
    print("=== TESTING EXACT CHART FIBONACCI RETRACEMENT (2026 AUDIT) ===\n")
    rows = []
    for name, params in grid:
        res = test_chart_fibonacci(**params)
        rows.append({
            'Setup': name,
            'Trades': res['trades'],
            'Win Rate': f"{res['wr']}%",
            'Gross (₹)': f"₹{res['gross']:,.1f}",
            'Fees (₹)': f"₹{res['fees']:,.1f}",
            'Net Realized (₹)': f"₹{res['net']:,.1f}",
            'Ending Equity (₹)': f"₹{res['equity']:,.1f}",
            'ROI (%)': f"{res['roi']:+.2f}%",
        })
        
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
