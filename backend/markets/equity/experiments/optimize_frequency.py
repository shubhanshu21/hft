#!/usr/bin/env python3
"""
markets/equity/experiments/optimize_frequency.py
Investigate why trade frequency is low on the Top 10 stocks and test parameter
refinements (lookback window, 38.2% retracement zone, ATR threshold) to increase
trade frequency and maximize total profits while maintaining high win rate.
"""
from __future__ import annotations

import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta

from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares
from markets.equity.experiments.fibonacci_retracement import get_symbol_file

top_stocks = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 
    'HINDALCO', 'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 
    'BHARTIARTL', 'SUNPHARMA'
]


def test_params(
    lookback: int = 10,
    fib_shallow: float = 0.382,
    fib_deep: float = 0.650,
    min_atr_mult: float = 1.0,
    rr_ratio: float = 2.0,
    risk_pct: float = 5.0,
):
    all_trades = []
    
    for sym in top_stocks:
        fpath = get_symbol_file(sym, 'daily')
        if not fpath:
            continue
            
        df = pd.read_csv(fpath)
        if df.empty or len(df) < lookback * 3:
            continue
            
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        
        opens = df['open'].to_numpy(dtype=np.float64)
        highs = df['high'].to_numpy(dtype=np.float64)
        lows = df['low'].to_numpy(dtype=np.float64)
        closes = df['close'].to_numpy(dtype=np.float64)
        atrs = df['atr'].fillna(df['close'] * 0.01).to_numpy(dtype=np.float64)
        ema50s = df['ema50'].fillna(df['close']).to_numpy(dtype=np.float64)
        dtimes = df['datetime'].to_numpy()
        
        n = len(df)
        
        # Rolling swing detection
        swing_high = np.zeros(n, dtype=bool)
        swing_low = np.zeros(n, dtype=bool)
        for idx in range(lookback, n - lookback):
            if highs[idx] == np.max(highs[idx - lookback : idx + lookback + 1]):
                swing_high[idx] = True
            if lows[idx] == np.min(lows[idx - lookback : idx + lookback + 1]):
                swing_low[idx] = True
                
        active_pos = None
        last_l_val, last_l_idx = None, None
        last_h_val, last_h_idx = None, None
        
        capital = 100000.0
        
        for i in range(lookback * 2, n):
            c_time = dtimes[i]
            c_close = closes[i]
            c_high = highs[i]
            c_low = lows[i]
            c_open = opens[i]
            c_atr = atrs[i]
            
            chk = i - lookback
            if swing_low[chk]:
                last_l_val = lows[chk]
                last_l_idx = chk
            if swing_high[chk]:
                last_h_val = highs[chk]
                last_h_idx = chk
                
            # Exit
            if active_pos is not None:
                sl = active_pos['sl']
                tp = active_pos['tp']
                exit_p = None
                exit_r = None
                if c_low <= sl:
                    exit_p = sl
                    exit_r = 'STOP_LOSS'
                elif c_high >= tp:
                    exit_p = tp
                    exit_r = 'TAKE_PROFIT'
                    
                if exit_p is not None:
                    cost = compute_nse_equity_delivery_costs('long', active_pos['entry_p'], exit_p, active_pos['qty'], symbol=sym)
                    all_trades.append({
                        'symbol': sym,
                        'entry_time': active_pos['entry_time'],
                        'exit_time': c_time,
                        'net_pnl': cost['net'],
                        'gross_pnl': cost['gross'],
                        'friction': cost['total'],
                        'reason': exit_r,
                    })
                    active_pos = None
                    
            # Entry
            if active_pos is None:
                if last_l_val and last_h_val and (last_h_idx > last_l_idx):
                    s_range = last_h_val - last_l_val
                    if s_range >= min_atr_mult * c_atr and c_close >= ema50s[i]:
                        # Fib levels
                        fib_top = last_h_val - fib_shallow * s_range
                        fib_bot = last_h_val - fib_deep * s_range
                        fib_inv = last_h_val - 0.786 * s_range
                        
                        in_zone = (c_low <= fib_top) and (c_low >= fib_inv)
                        bar_rng = max(c_high - c_low, 1e-4)
                        # Rejection confirmation
                        bull_rejection = (c_close > c_open) and (c_close > fib_bot) and ((c_close - c_low) / bar_rng >= 0.40)
                        
                        if in_zone and bull_rejection:
                            entry_p = c_close
                            sl = round(last_l_val - 0.2 * c_atr, 2)
                            s_dist = entry_p - sl
                            if s_dist > 0:
                                tp = round(entry_p + s_dist * rr_ratio, 2)
                                qty = size_equity_shares(capital, entry_p, s_dist, risk_pct, leverage=1.0)
                                if qty > 0:
                                    active_pos = {
                                        'entry_p': entry_p,
                                        'sl': sl,
                                        'tp': tp,
                                        'qty': qty,
                                        'entry_time': c_time,
                                    }
                                    
    # Sort chronologically
    all_trades.sort(key=lambda t: t['exit_time'])
    
    # 2026 filter
    t_2026 = [t for t in all_trades if str(t['exit_time'])[:4] == '2026']
    
    tot_trades_all = len(all_trades)
    wins_all = sum(1 for t in all_trades if t['net_pnl'] > 0)
    net_all = sum(t['net_pnl'] for t in all_trades)
    
    tot_2026 = len(t_2026)
    wins_2026 = sum(1 for t in t_2026 if t['net_pnl'] > 0)
    net_2026 = sum(t['net_pnl'] for t in t_2026)
    
    # Compounding calculation on 2026 trades from ₹1 Lakh
    comp_2026 = 100000.0
    for t in t_2026:
        pct = t['net_pnl'] / 100000.0
        comp_2026 += comp_2026 * pct
        
    return {
        'lookback': lookback,
        'shallow': fib_shallow,
        'deep': fib_deep,
        'atr_mult': min_atr_mult,
        'trades_all': tot_trades_all,
        'wr_all': round(wins_all / tot_trades_all * 100, 1) if tot_trades_all else 0,
        'net_all': round(net_all, 1),
        'trades_2026': tot_2026,
        'wr_2026': round(wins_2026 / tot_2026 * 100, 1) if tot_2026 else 0,
        'net_2026': round(net_2026, 1),
        'comp_2026': round(comp_2026, 1),
    }


if __name__ == '__main__':
    configs = [
        # Base setup (our current strict config)
        {'lookback': 15, 'fib_shallow': 0.500, 'fib_deep': 0.618, 'min_atr_mult': 1.5},
        # Test 1: Faster Swing detection (lookback 10 instead of 15)
        {'lookback': 10, 'fib_shallow': 0.500, 'fib_deep': 0.618, 'min_atr_mult': 1.5},
        # Test 2: Faster Swing detection (lookback 8)
        {'lookback': 8, 'fib_shallow': 0.500, 'fib_deep': 0.618, 'min_atr_mult': 1.5},
        # Test 3: Allow 38.2% shallow Fibonacci pullbacks (lookback 10)
        {'lookback': 10, 'fib_shallow': 0.382, 'fib_deep': 0.618, 'min_atr_mult': 1.2},
        # Test 4: Allow 38.2% shallow Fibonacci pullbacks (lookback 8)
        {'lookback': 8, 'fib_shallow': 0.382, 'fib_deep': 0.618, 'min_atr_mult': 1.0},
        # Test 5: Standard Golden Zone with 1.0 ATR (lookback 10)
        {'lookback': 10, 'fib_shallow': 0.500, 'fib_deep': 0.618, 'min_atr_mult': 1.0},
    ]
    
    rows = []
    for c in configs:
        res = test_params(**c)
        rows.append(res)
        
    df_res = pd.DataFrame(rows)
    print('=== TRADE FREQUENCY & PROFITABILITY OPTIMIZATION (TOP 10 STOCKS) ===\n')
    print(df_res.to_string(index=False))
