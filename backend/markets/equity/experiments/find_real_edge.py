#!/usr/bin/env python3
"""
True Single-Account Portfolio Edge Discovery (Fast O(1) Implementation).
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
        
        records = df.set_index('date_str').to_dict(orient='index')
        date_to_idx = {d: i for i, d in enumerate(df['date_str'])}
        symbol_dict[sym] = (records, df, date_to_idx)
    return symbol_dict

ALL_DATA = load_data()


def simulate_portfolio(
    symbols=NIFTY50_SYMBOLS,
    start_cap=100000.0,
    max_active_positions=4,
    max_cap_per_pos_pct=0.25,
    risk_per_trade_pct=2.0,
    rr_ratio=2.5,
    trail_to_be_at_1r=True,
    start_year='2026',
):
    valid_syms = [s for s in symbols if s in ALL_DATA]
    
    dates_set = set()
    for s in valid_syms:
        records, _, _ = ALL_DATA[s]
        for d in records.keys():
            if d.startswith(start_year):
                dates_set.add(d)
    all_dates = sorted(list(dates_set))
    
    cash = start_cap
    open_positions = {}
    closed_trades = []
    
    state = {}
    for sym in valid_syms:
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
        # 1. Manage Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r:
                continue
            c_high = r['high']
            c_low = r['low']
            
            if trail_to_be_at_1r:
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

        # 2. Find Candidates
        candidate_entries = []
        for sym in valid_syms:
            records, df, date_to_idx = ALL_DATA[sym]
            r = records.get(current_date)
            if not r:
                continue
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
                                'orig_sl': sl,
                                'tp': tp,
                                's_dist': s_dist,
                            })

        # 3. Enter Positions
        port_equity = cash + sum(p['qty'] * p['entry_p'] for p in open_positions.values())
        max_alloc_per_pos = port_equity * max_cap_per_pos_pct
        
        for cand in candidate_entries:
            if len(open_positions) >= max_active_positions:
                break
            risk_cash = port_equity * (risk_per_trade_pct / 100.0)
            qty_by_risk = int(risk_cash / cand['s_dist'])
            qty_by_cap = int(max_alloc_per_pos / cand['entry_p'])
            target_qty = min(qty_by_risk, qty_by_cap)
            
            actual_qty = min(target_qty, int(cash / cand['entry_p']))
            if actual_qty > 0:
                cost = actual_qty * cand['entry_p']
                cash -= cost
                open_positions[cand['sym']] = {
                    'entry_p': cand['entry_p'],
                    'sl': cand['sl'],
                    'orig_sl': cand['orig_sl'],
                    'tp': cand['tp'],
                    'qty': actual_qty,
                    'entry_date': current_date,
                }

    # Mark to market
    unreal_total = 0.0
    for sym, pos in open_positions.items():
        _, df, _ = ALL_DATA[sym]
        last_p = df.iloc[-1]['close']
        unreal_total += (last_p - pos['entry_p']) * pos['qty']
        
    final_equity = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    
    wins = sum(1 for t in closed_trades if t['Net'] > 0)
    net_closed = sum(t['Net'] for t in closed_trades)
    
    return {
        'closed': len(closed_trades),
        'open': len(open_positions),
        'wins': wins,
        'wr': round(wins / len(closed_trades) * 100, 1) if closed_trades else 0,
        'closed_net': round(net_closed, 1),
        'unrealized': round(unreal_total, 1),
        'final_equity': round(final_equity, 1),
        'roi_pct': round((final_equity - start_cap) / start_cap * 100, 2),
    }


if __name__ == '__main__':
    grid = [
        {"name": "1. Baseline (1 Trade, 100% Cash, 2:1 RR)", "max_active_positions": 1, "max_cap_per_pos_pct": 1.0, "risk_per_trade_pct": 5.0, "rr_ratio": 2.0, "trail_to_be_at_1r": False},
        {"name": "2. Baseline + Trail BE @ +1R", "max_active_positions": 1, "max_cap_per_pos_pct": 1.0, "risk_per_trade_pct": 5.0, "rr_ratio": 2.0, "trail_to_be_at_1r": True},
        {"name": "3. 3 Positions (33% cap, 2% risk, 2:1 RR)", "max_active_positions": 3, "max_cap_per_pos_pct": 0.33, "risk_per_trade_pct": 2.0, "rr_ratio": 2.0, "trail_to_be_at_1r": True},
        {"name": "4. 4 Positions (25% cap, 2% risk, 2:1 RR)", "max_active_positions": 4, "max_cap_per_pos_pct": 0.25, "risk_per_trade_pct": 2.0, "rr_ratio": 2.0, "trail_to_be_at_1r": True},
        {"name": "5. 4 Positions (25% cap, 2% risk, 2.5:1 RR)", "max_active_positions": 4, "max_cap_per_pos_pct": 0.25, "risk_per_trade_pct": 2.0, "rr_ratio": 2.5, "trail_to_be_at_1r": True},
        {"name": "6. 4 Positions (25% cap, 2% risk, 3.0:1 RR)", "max_active_positions": 4, "max_cap_per_pos_pct": 0.25, "risk_per_trade_pct": 2.0, "rr_ratio": 3.0, "trail_to_be_at_1r": True},
        {"name": "7. 5 Positions (20% cap, 1.5% risk, 2.5:1 RR)", "max_active_positions": 5, "max_cap_per_pos_pct": 0.20, "risk_per_trade_pct": 1.5, "rr_ratio": 2.5, "trail_to_be_at_1r": True},
    ]
    
    rows = []
    for g in grid:
        params = dict(g)
        name = params.pop('name')
        res = simulate_portfolio(**params)
        res['Configuration'] = name
        rows.append(res)
        
    df_res = pd.DataFrame(rows)
    cols = ['Configuration', 'closed', 'open', 'wr', 'closed_net', 'unrealized', 'final_equity', 'roi_pct']
    print(df_res[cols].to_string(index=False))
