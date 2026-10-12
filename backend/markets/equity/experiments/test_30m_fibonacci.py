#!/usr/bin/env python3
"""
Test 30-minute Fibonacci Retracement Strategy on 2026 (Jan 1 - Oct 9, 2026).
- Sized for ₹100,000 capital.
- Tests if higher frequency (30m candles) solves the idle capital problem and increases net profits.
- Compares:
  1. 30m timeframe on top momentum leaders
  2. With intraday MIS vs delivery costs
"""
import glob
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_costs, compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"

def load_30m_data():
    symbol_dict = {}
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_30minute.csv"
        try:
            df = pd.read_csv(f)
            df['datetime'] = pd.to_datetime(df['timestamp'])
            df = df.sort_values('datetime').reset_index(drop=True)
            if len(df) < 100: continue
            
            df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
            df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            df['ema50'] = ta.ema(df['close'], length=50)
            df['ema200'] = ta.ema(df['close'], length=200)
            
            symbol_dict[sym] = df
        except Exception:
            continue
    return symbol_dict

ALL_30M = load_30m_data()

def run_30m_backtest(
    top_n_leaders=5,
    max_active_pos=2,
    max_alloc_pct=0.50,
    swing_lookback=10,
    min_swing_pct=0.03,
    start_year='2026',
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in ALL_30M]
    
    # Align dates
    timestamps_set = set()
    for s in symbols:
        df = ALL_30M[s]
        df_2026 = df[df['date_str'] >= f"{start_year}-01-01"]
        for ts in df_2026['timestamp']:
            timestamps_set.add(ts)
    all_timestamps = sorted(list(timestamps_set))
    
    cash = 100000.0
    open_positions = {}
    closed_trades = []
    
    # Pre-index by timestamp
    indexed_data = {}
    for s in symbols:
        df = ALL_30M[s]
        indexed_data[s] = df.set_index('timestamp').to_dict('index')
        
    for current_ts in all_timestamps:
        current_date = current_ts[:10]
        
        # 1. Check Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            r = indexed_data[sym].get(current_ts)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            # Trail stop to BE at +1R
            risk_unit = pos['entry_p'] - pos['orig_sl']
            if risk_unit > 0 and c_high >= (pos['entry_p'] + risk_unit):
                pos['sl'] = max(pos['sl'], pos['entry_p'])
                
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
                net_proceeds = (pos['qty'] * exit_p) - cost['total']
                cash += net_proceeds
                curr_eq = cash + sum(p['qty'] * indexed_data[s].get(current_ts, {}).get('close', p['entry_p']) for s, p in open_positions.items() if s != sym)
                
                closed_trades.append({
                    'Stock': sym,
                    'Bought': pos['entry_ts'],
                    'Sold': current_ts,
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': round(cost['gross'], 1),
                    'Fees': round(cost['total'], 1),
                    'Net': round(cost['net'], 1),
                    'Equity': round(curr_eq, 1),
                    'Result': exit_r,
                })
                del open_positions[sym]
                
        # 2. Check Entries
        if len(open_positions) < max_active_pos:
            candidates = []
            for sym in symbols:
                if sym in open_positions: continue
                df = ALL_30M[sym]
                r = indexed_data[sym].get(current_ts)
                if not r: continue
                
                # Check uptrend
                c_close = r['close']
                ema50 = r.get('ema50')
                if pd.isna(ema50) or c_close < ema50: continue
                
                # Check swings from past bars
                # Find recent bars up to current_ts
                c_idx = df[df['timestamp'] == current_ts].index
                if len(c_idx) == 0: continue
                idx = c_idx[0]
                if idx < swing_lookback * 3: continue
                
                sub_highs = df.loc[idx - 60 : idx - 1, 'high'].to_numpy()
                sub_lows = df.loc[idx - 60 : idx - 1, 'low'].to_numpy()
                
                peaks = argrelextrema(sub_highs, np.greater_equal, order=swing_lookback)[0]
                troughs = argrelextrema(sub_lows, np.less_equal, order=swing_lookback)[0]
                if len(peaks) == 0 or len(troughs) == 0: continue
                
                h_val = float(sub_highs[peaks[-1]])
                l_val = float(sub_lows[troughs[-1]])
                if peaks[-1] <= troughs[-1]: continue
                
                swing_size = h_val - l_val
                if swing_size / l_val < min_swing_pct: continue
                
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                
                c_low = r['low']
                c_high = r['high']
                c_open = r['open']
                
                if c_low <= fib_500 and c_high >= fib_618:
                    if (c_close > c_open) or ((min(c_open, c_close) - c_low) / max(c_high - c_low, 1e-4) >= 0.25):
                        entry_p = c_close
                        atr = r['atr'] if pd.notna(r['atr']) else (0.01 * entry_p)
                        sl = min(fib_618, c_low) - (0.3 * atr)
                        tp = h_val
                        if (tp - entry_p) / (entry_p - sl) >= 1.0:
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'tp': tp,
                                'ts': current_ts,
                            })
                            
            # Allocate
            port_equity = cash + sum(p['qty'] * indexed_data[s].get(current_ts, {}).get('close', p['entry_p']) for s, p in open_positions.items())
            max_alloc = port_equity * max_alloc_pct
            for cand in candidates:
                if len(open_positions) >= max_active_pos: break
                qty = min(int(max_alloc / cand['entry_p']), int(cash / cand['entry_p']))
                if qty > 0:
                    cost_est = qty * cand['entry_p']
                    if cash >= cost_est:
                        cash -= cost_est
                        open_positions[cand['sym']] = {
                            'entry_p': cand['entry_p'],
                            'sl': cand['sl'],
                            'orig_sl': cand['sl'],
                            'tp': cand['tp'],
                            'qty': qty,
                            'entry_ts': cand['ts'],
                        }

    # Final totals
    final_eq = cash + sum(p['qty'] * ALL_30M[s].iloc[-1]['close'] for s, p in open_positions.items())
    wins = [t for t in closed_trades if t['Net'] > 0]
    wr = round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0
    return {
        'trades': len(closed_trades),
        'wins': len(wins),
        'wr': wr,
        'gross': round(sum(t['Gross'] for t in closed_trades), 1),
        'fees': round(sum(t['Fees'] for t in closed_trades), 1),
        'net': round(sum(t['Net'] for t in closed_trades), 1),
        'final_equity': round(final_eq, 1),
        'roi_pct': round((final_eq - 100000.0) / 1000.0, 2),
        'closed_trades': closed_trades,
    }

if __name__ == '__main__':
    print("Testing 30-minute Fibonacci Retracements for 2026...")
    res = run_30m_backtest(min_swing_pct=0.03, swing_lookback=8)
    print(f"30m Results: Trades = {res['trades']}, Win Rate = {res['wr']}%, Gross = ₹{res['gross']}, Fees = ₹{res['fees']}, Net Realized = ₹{res['net']}, Final Equity = ₹{res['final_equity']} ({res['roi_pct']:+}%)")
