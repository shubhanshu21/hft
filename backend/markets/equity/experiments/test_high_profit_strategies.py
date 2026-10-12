#!/usr/bin/env python3
"""
Test Alternative High-Expectancy Strategies on 2026 Data for ₹100,000 Capital.
Tests:
1. Daily 200 EMA + RSI Oversold Mean Reversion (Larry Connors style):
   Buy when Close > 200 EMA and RSI(4) < 25. Exit when RSI(4) > 70 or after 5 days.
2. 20-Day High Momentum Breakout (Turtle style):
   Buy when Close breaks above 20-day high. Exit when Close drops below 10-day low.
3. 30-Minute Intraday Trend Following (MIS with 0 DP charges).
"""
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs, compute_nse_equity_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"

def load_data():
    symbols_data = {}
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_daily.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        if len(df) < 100: continue
        
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['rsi4'] = ta.rsi(df['close'], length=4)
        df['rsi14'] = ta.rsi(df['close'], length=14)
        df['high20'] = df['high'].rolling(20).max()
        df['low10'] = df['low'].rolling(10).min()
        
        symbols_data[sym] = df
    return symbols_data

ALL_DATA = load_data()

def run_mean_reversion_2026(rsi_entry=30, max_pos=3, alloc_pct=0.33):
    """Buy oversold dips in macro uptrends (Close > EMA 200). Exit when RSI recovers > 65."""
    symbols = list(ALL_DATA.keys())
    dates = sorted(list(set(d for s in symbols for d in ALL_DATA[s][ALL_DATA[s]['date_str'] >= '2026-01-01']['date_str'])))
    
    cash = 100000.0
    open_pos = {}
    closed = []
    
    recs = {s: ALL_DATA[s].set_index('date_str').to_dict('index') for s in symbols}
    
    for d in dates:
        # Exits
        for s in list(open_pos.keys()):
            pos = open_pos[s]
            r = recs[s].get(d)
            if not r: continue
            
            c_close, c_low, c_high = r['close'], r['low'], r['high']
            rsi = r.get('rsi4', 50)
            
            exit_p = None
            exit_r = None
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                exit_r = 'stop'
            elif rsi >= 65 or c_close >= pos['tp']:
                exit_p = c_close
                exit_r = 'target'
            elif pos['bars_held'] >= 8:
                exit_p = c_close
                exit_r = 'time_exit'
                
            if exit_p:
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed.append({
                    'Stock': s, 'Net': cost['net'], 'Gross': cost['gross'],
                    'Fees': cost['total'], 'Result': exit_r,
                })
                del open_pos[s]
            else:
                pos['bars_held'] += 1
                
        # Entries
        if len(open_pos) < max_pos:
            cands = []
            for s in symbols:
                if s in open_pos: continue
                r = recs[s].get(d)
                if not r: continue
                
                close = r['close']
                ema200 = r.get('ema200')
                rsi = r.get('rsi4')
                atr = r.get('atr')
                if pd.isna(ema200) or pd.isna(rsi) or pd.isna(atr): continue
                
                # Uptrend + Oversold pullback
                if close > ema200 and rsi < rsi_entry:
                    sl = round(close - 1.5 * atr, 2)
                    tp = round(close + 2.0 * (close - sl), 2)
                    cands.append({'s': s, 'entry_p': close, 'sl': sl, 'tp': tp, 'rsi': rsi})
                    
            cands.sort(key=lambda x: x['rsi'])
            eq = cash + sum(p['qty'] * p['entry_p'] for p in open_pos.values())
            alloc = eq * alloc_pct
            for c in cands:
                if len(open_pos) >= max_pos: break
                qty = min(int(alloc / c['entry_p']), int(cash / c['entry_p']))
                if qty > 0:
                    cash -= qty * c['entry_p']
                    open_pos[c['s']] = {
                        'entry_p': c['entry_p'], 'sl': c['sl'], 'tp': c['tp'],
                        'qty': qty, 'bars_held': 0,
                    }
                    
    wins = [t for t in closed if t['Net'] > 0]
    final_eq = cash + sum(p['qty'] * ALL_DATA[s].iloc[-1]['close'] for s, p in open_pos.items())
    return {
        'trades': len(closed),
        'wins': len(wins),
        'wr': round(len(wins)/len(closed)*100, 1) if closed else 0,
        'gross': round(sum(t['Gross'] for t in closed), 1),
        'fees': round(sum(t['Fees'] for t in closed), 1),
        'net': round(sum(t['Net'] for t in closed), 1),
        'equity': round(final_eq, 1),
        'roi': round((final_eq - 100000)/1000, 2),
    }

def test_momentum_breakout_2026(max_pos=3, alloc_pct=0.33):
    """Buy 20-day high breakouts above 50 EMA. Exit when below 10-day low."""
    symbols = list(ALL_DATA.keys())
    dates = sorted(list(set(d for s in symbols for d in ALL_DATA[s][ALL_DATA[s]['date_str'] >= '2026-01-01']['date_str'])))
    
    cash = 100000.0
    open_pos = {}
    closed = []
    
    recs = {s: ALL_DATA[s].set_index('date_str').to_dict('index') for s in symbols}
    
    for d in dates:
        # Exits
        for s in list(open_pos.keys()):
            pos = open_pos[s]
            r = recs[s].get(d)
            if not r: continue
            
            c_close, c_low, c_high = r['close'], r['low'], r['high']
            low10 = r.get('low10', pos['sl'])
            
            exit_p = None
            exit_r = None
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                exit_r = 'stop'
            elif c_close < low10:
                exit_p = c_close
                exit_r = 'trend_exit'
                
            if exit_p:
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed.append({
                    'Stock': s, 'Net': cost['net'], 'Gross': cost['gross'],
                    'Fees': cost['total'], 'Result': exit_r,
                })
                del open_pos[s]
                
        # Entries
        if len(open_pos) < max_pos:
            cands = []
            for s in symbols:
                if s in open_pos: continue
                r = recs[s].get(d)
                if not r: continue
                
                close = r['close']
                ema50 = r.get('ema50')
                high20 = r.get('high20')
                atr = r.get('atr')
                if pd.isna(ema50) or pd.isna(high20) or pd.isna(atr): continue
                
                # Breakout
                if close >= high20 and close > ema50:
                    sl = round(close - 2.0 * atr, 2)
                    cands.append({'s': s, 'entry_p': close, 'sl': sl})
                    
            eq = cash + sum(p['qty'] * p['entry_p'] for p in open_pos.values())
            alloc = eq * alloc_pct
            for c in cands:
                if len(open_pos) >= max_pos: break
                qty = min(int(alloc / c['entry_p']), int(cash / c['entry_p']))
                if qty > 0:
                    cash -= qty * c['entry_p']
                    open_pos[c['s']] = {
                        'entry_p': c['entry_p'], 'sl': c['sl'],
                        'qty': qty,
                    }
                    
    wins = [t for t in closed if t['Net'] > 0]
    final_eq = cash + sum(p['qty'] * ALL_DATA[s].iloc[-1]['close'] for s, p in open_pos.items())
    return {
        'trades': len(closed),
        'wins': len(wins),
        'wr': round(len(wins)/len(closed)*100, 1) if closed else 0,
        'gross': round(sum(t['Gross'] for t in closed), 1),
        'fees': round(sum(t['Fees'] for t in closed), 1),
        'net': round(sum(t['Net'] for t in closed), 1),
        'equity': round(final_eq, 1),
        'roi': round((final_eq - 100000)/1000, 2),
    }

if __name__ == '__main__':
    print("=== TESTING ALTERNATIVE EQUITY EDGES ON REAL 2026 DATA (₹100,000 CAPITAL) ===\n")
    mr1 = test_mean_reversion_2026(rsi_entry=30, max_pos=3)
    mr2 = test_mean_reversion_2026(rsi_entry=25, max_pos=2, alloc_pct=0.50)
    tb1 = test_momentum_breakout_2026(max_pos=3)
    
    rows = [
        {'Strategy': '1. Mean Reversion Pullbacks (RSI<30, Close>EMA200, 3 Pos)', **mr1},
        {'Strategy': '2. Mean Reversion Pullbacks (RSI<25, Close>EMA200, 2 Pos)', **mr2},
        {'Strategy': '3. 20-Day High Breakout Trend Following (3 Pos)', **tb1},
    ]
    df = pd.DataFrame(rows)
    cols = ['Strategy', 'trades', 'wr', 'gross', 'fees', 'net', 'equity', 'roi']
    print(df[cols].to_string(index=False))
