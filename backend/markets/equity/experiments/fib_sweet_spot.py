#!/usr/bin/env python3
"""
Fibonacci Sweet Spot Discovery for Indian Equities.
Explores:
- Meaningful Impulse Thresholds: 5%, 7%, 10% swing range (institutional wave vs noise)
- Volume confirmation on pullback reversal
- Target calibration: Prior Swing High vs 1.5R vs 2.0R
- Single-account capital constraints (₹100k, 2-3 positions max)
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
        df['ema20'] = ta.ema(df['close'], length=20)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['vol_sma20'] = ta.sma(df['volume'], length=20)
        
        # Precompute swings (lookback 15)
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


def test_sweet_spot(
    min_swing_pct=0.06,          # 6% minimum impulse wave
    fib_shallow=0.500,           # 50% retracement
    fib_deep=0.618,             # 61.8% retracement
    target_mode='prior_high',    # 'prior_high', 'rr1.5', 'rr2.0'
    trail_be_at_1r=True,
    require_volume=False,
    max_active_pos=3,
    max_alloc_pct=0.33,
    start_year='2026',
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in ALL_DATA]
    
    dates_set = set()
    for s in symbols:
        records, _, _ = ALL_DATA[s]
        for d in records.keys():
            if d.startswith(start_year):
                dates_set.add(d)
    all_dates = sorted(list(dates_set))
    
    cash = 100000.0
    open_positions = {}
    closed_trades = []
    
    state = {}
    for sym in symbols:
        records, df, _ = ALL_DATA[sym]
        lookback = 15
        df_pre = df[df['date_str'] < f"{start_year}-01-01"]
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
        # Exits
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
                    'Result': exit_r,
                    'Net': cost['net'],
                    'Fees': cost['total']
                })
                del open_positions[sym]

        # Entries
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
                vol_ok = (r['volume'] >= r['vol_sma20']) if require_volume and pd.notna(r['vol_sma20']) else True
                
                # Check institutional wave size & trend
                if impulse_pct >= min_swing_pct and c_close >= c_ema50 and vol_ok:
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
                            if target_mode == 'prior_high':
                                tp = round(h_val, 2)
                            elif target_mode == 'rr1.5':
                                tp = round(entry_p + s_dist * 1.5, 2)
                            else:
                                tp = round(entry_p + s_dist * 2.0, 2)
                                
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'orig_sl': sl,
                                'tp': tp,
                                's_dist': s_dist,
                            })

        # Sizing & Allocating
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
                }

    # Mark to market
    unreal = 0.0
    for sym, pos in open_positions.items():
        _, df, _ = ALL_DATA[sym]
        last_p = df.iloc[-1]['close']
        unreal += (last_p - pos['entry_p']) * pos['qty']
        
    final_eq = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    wins = sum(1 for t in closed_trades if t['Net'] > 0)
    
    return {
        'closed': len(closed_trades),
        'open': len(open_positions),
        'wins': wins,
        'wr': round(wins / len(closed_trades) * 100, 1) if closed_trades else 0,
        'realized': round(sum(t['Net'] for t in closed_trades), 1),
        'unrealized': round(unreal, 1),
        'final_equity': round(final_eq, 1),
        'roi_pct': round((final_eq - 100000.0) / 1000.0, 2),
    }


if __name__ == '__main__':
    grid = [
        # Impulse swing size tests
        {"name": "1. Standard 6% Impulse, Prior High Target", "min_swing_pct": 0.06, "target_mode": 'prior_high', "max_active_pos": 3},
        {"name": "2. Strong 8% Impulse, Prior High Target", "min_swing_pct": 0.08, "target_mode": 'prior_high', "max_active_pos": 3},
        {"name": "3. Major 10% Impulse, Prior High Target", "min_swing_pct": 0.10, "target_mode": 'prior_high', "max_active_pos": 3},
        # Target comparisons
        {"name": "4. Strong 8% Impulse, 1.5R Target", "min_swing_pct": 0.08, "target_mode": 'rr1.5', "max_active_pos": 3},
        {"name": "5. Strong 8% Impulse, 2.0R Target", "min_swing_pct": 0.08, "target_mode": 'rr2.0', "max_active_pos": 3},
        # Golden Pocket vs OTE
        {"name": "6. Strong 8% Impulse + OTE (61.8%-70.5%)", "min_swing_pct": 0.08, "fib_shallow": 0.618, "fib_deep": 0.705, "target_mode": 'prior_high', "max_active_pos": 3},
        # Volume filter
        {"name": "7. Strong 8% Impulse + Vol Surge", "min_swing_pct": 0.08, "target_mode": 'prior_high', "require_volume": True, "max_active_pos": 3},
        # 2 Concentrated Positions (50% each)
        {"name": "8. Strong 8% Impulse, 2 Positions (50% each)", "min_swing_pct": 0.08, "target_mode": 'prior_high', "max_active_pos": 2, "max_alloc_pct": 0.50},
    ]
    
    rows = []
    for g in grid:
        params = dict(g)
        name = params.pop('name')
        res = test_sweet_spot(**params)
        res['Configuration'] = name
        rows.append(res)
        
    df = pd.DataFrame(rows)
    cols = ['Configuration', 'closed', 'open', 'wr', 'realized', 'unrealized', 'final_equity', 'roi_pct']
    print(df[cols].to_string(index=False))
