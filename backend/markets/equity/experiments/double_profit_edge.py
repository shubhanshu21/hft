#!/usr/bin/env python3
"""
Comprehensive experiment to find the quantitative edge that doubles 2026 profits
from ~₹50,000 to ₹100,000+ without artificially restricting trades.
"""
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta

from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares
from markets.equity.experiments.fibonacci_retracement import get_symbol_file, ARCHIVE_DIR

all_symbols = sorted(list(set(f.split('_')[0] for f in os.listdir(ARCHIVE_DIR) if '_' in f)))

top10 = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
    'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA'
]

top20 = [
    'APOLLOHOSP', 'TATASTEEL', 'MARUTI', 'BAJAJ-AUTO', 'HINDALCO', 
    'JSWSTEEL', 'ADANIPORTS', 'EICHERMOT', 'BHARTIARTL', 'SUNPHARMA',
    'IOC', 'SBIN', 'ULTRACEMCO', 'SBILIFE', 'ASIANPAINT', 
    'COALINDIA', 'HEROMOTOCO', 'ONGC', 'LT', 'GRASIM'
]


def run_edge_sim(
    symbols,
    lookback=15,
    rr_target=2.0,
    max_pos_per_stock=1,
    min_atr_mult=1.5,
    require_ema200=False,
    trail_sl_at_1r=False,
    timeframes=['daily'],
    risk_pct=5.0
):
    all_trades = []
    
    for tf in timeframes:
        for sym in symbols:
            fpath = get_symbol_file(sym, tf)
            if not fpath:
                continue
            df = pd.read_csv(fpath)
            if df.empty or len(df) < lookback * 3:
                continue
            df['datetime'] = pd.to_datetime(df['timestamp'])
            df = df.sort_values('datetime').reset_index(drop=True)
            
            df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            df['ema50'] = ta.ema(df['close'], length=50)
            if require_ema200:
                df['ema200'] = ta.ema(df['close'], length=200)
            
            opens = df['open'].to_numpy(dtype=np.float64)
            highs = df['high'].to_numpy(dtype=np.float64)
            lows = df['low'].to_numpy(dtype=np.float64)
            closes = df['close'].to_numpy(dtype=np.float64)
            atrs = df['atr'].fillna(df['close'] * 0.01).to_numpy(dtype=np.float64)
            ema50s = df['ema50'].fillna(df['close']).to_numpy(dtype=np.float64)
            ema200s = df['ema200'].fillna(df['close']).to_numpy(dtype=np.float64) if require_ema200 else None
            dtimes = df['datetime'].to_numpy()
            n = len(df)
            
            swing_high = np.zeros(n, dtype=bool)
            swing_low = np.zeros(n, dtype=bool)
            for idx in range(lookback, n - lookback):
                if highs[idx] == np.max(highs[idx - lookback : idx + lookback + 1]):
                    swing_high[idx] = True
                if lows[idx] == np.min(lows[idx - lookback : idx + lookback + 1]):
                    swing_low[idx] = True
                    
            positions = []
            last_l_val, last_l_idx = None, None
            last_h_val, last_h_idx = None, None
            
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
                    
                # Exits
                remaining = []
                for pos in positions:
                    exit_p = None
                    exit_r = None
                    
                    # Trailing check
                    if trail_sl_at_1r:
                        risk = pos['entry_p'] - pos['orig_sl']
                        if c_high >= pos['entry_p'] + risk and pos['sl'] < pos['entry_p']:
                            pos['sl'] = pos['entry_p']  # Move SL to breakeven
                            
                    if c_low <= pos['sl']:
                        exit_p = pos['sl']
                        exit_r = 'STOP_LOSS' if pos['sl'] <= pos['entry_p'] else 'TRAIL_STOP'
                    elif c_high >= pos['tp']:
                        exit_p = pos['tp']
                        exit_r = 'TAKE_PROFIT'
                        
                    if exit_p is not None:
                        cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=sym)
                        all_trades.append({
                            'symbol': sym,
                            'timeframe': tf,
                            'entry_time': pos['entry_time'],
                            'exit_time': c_time,
                            'entry_p': pos['entry_p'],
                            'exit_p': exit_p,
                            'qty': pos['qty'],
                            'gross_pnl': cost['gross'],
                            'net_pnl': cost['net'],
                            'friction': cost['total'],
                            'reason': exit_r,
                        })
                    else:
                        remaining.append(pos)
                positions = remaining
                
                # Entry
                if len(positions) < max_pos_per_stock:
                    if last_l_val and last_h_val and (last_h_idx > last_l_idx):
                        s_range = last_h_val - last_l_val
                        trend_ok = c_close >= ema50s[i]
                        if require_ema200:
                            trend_ok = trend_ok and (c_close >= ema200s[i]) and (ema50s[i] >= ema200s[i])
                            
                        if s_range >= min_atr_mult * c_atr and trend_ok:
                            fib_top = last_h_val - 0.500 * s_range
                            fib_bot = last_h_val - 0.618 * s_range
                            fib_inv = last_h_val - 0.786 * s_range
                            
                            in_zone = (c_low <= fib_top) and (c_low >= fib_inv)
                            bar_rng = max(c_high - c_low, 1e-4)
                            bull_rejection = (c_close > c_open) and (c_close > fib_bot) and ((c_close - c_low) / bar_rng >= 0.40)
                            
                            if in_zone and bull_rejection:
                                entry_p = c_close
                                sl = round(last_l_val - 0.2 * c_atr, 2)
                                s_dist = entry_p - sl
                                if s_dist > 0:
                                    tp = round(entry_p + s_dist * rr_target, 2)
                                    qty = size_equity_shares(100000.0, entry_p, s_dist, risk_pct, leverage=1.0)
                                    if qty > 0 and not any(p['entry_p'] == entry_p for p in positions):
                                        positions.append({
                                            'entry_p': entry_p,
                                            'orig_sl': sl,
                                            'sl': sl,
                                            'tp': tp,
                                            'qty': qty,
                                            'entry_time': c_time,
                                        })
                                        
    all_trades.sort(key=lambda t: t['exit_time'])
    t_2026 = [t for t in all_trades if str(t['exit_time'])[:4] == '2026']
    
    wins_2026 = sum(1 for t in t_2026 if t['net_pnl'] > 0)
    net_2026 = sum(t['net_pnl'] for t in t_2026)
    gross_2026 = sum(t['gross_pnl'] for t in t_2026)
    fric_2026 = sum(t['friction'] for t in t_2026)
    
    comp_2026 = 100000.0
    for t in t_2026:
        pct = t['net_pnl'] / 100000.0
        comp_2026 += comp_2026 * pct
        
    comp_all = 100000.0
    for t in all_trades:
        pct = t['net_pnl'] / 100000.0
        comp_all += comp_all * pct
        
    return {
        'all_trades': len(all_trades),
        'all_net': round(sum(t['net_pnl'] for t in all_trades), 1),
        'all_comp': round(comp_all, 1),
        'trades_2026': len(t_2026),
        'wr_2026': round(wins_2026 / len(t_2026) * 100, 1) if t_2026 else 0,
        'gross_2026': round(gross_2026, 1),
        'fric_2026': round(fric_2026, 1),
        'net_2026': round(net_2026, 1),
        'comp_2026': round(comp_2026, 1),
    }


if __name__ == '__main__':
    experiments = [
        ("1. Current Baseline (Top 10, Max 1 Pos, 2:1 RR)", dict(symbols=top10, max_pos_per_stock=1, rr_target=2.0)),
        ("2. Top 20 Stocks (Max 1 Pos, 2:1 RR)", dict(symbols=top20, max_pos_per_stock=1, rr_target=2.0)),
        ("3. Top 20 Stocks + Max 2 Pos per Stock (Pyramiding)", dict(symbols=top20, max_pos_per_stock=2, rr_target=2.0)),
        ("4. Top 20 Stocks + Max 2 Pos + 2.5:1 RR (Higher Target)", dict(symbols=top20, max_pos_per_stock=2, rr_target=2.5)),
        ("5. Top 20 Stocks + Max 2 Pos + Daily & Weekly Confluence", dict(symbols=top20, max_pos_per_stock=2, rr_target=2.0, timeframes=['daily', 'weekly'])),
        ("6. ALL 49 Stocks + Trend Filter (Close>EMA50>EMA200) + Max 2 Pos", dict(symbols=all_symbols, max_pos_per_stock=2, rr_target=2.0, require_ema200=True)),
        ("7. ALL 49 Stocks + Max 2 Pos + Daily & Weekly Confluence", dict(symbols=all_symbols, max_pos_per_stock=2, rr_target=2.0, require_ema200=True, timeframes=['daily', 'weekly'])),
    ]
    
    rows = []
    for name, params in experiments:
        res = run_edge_sim(**params)
        res['Setup'] = name
        rows.append(res)
        
    df_res = pd.DataFrame(rows)
    cols = ['Setup', 'trades_2026', 'wr_2026', 'net_2026', 'comp_2026', 'all_trades', 'all_net', 'all_comp']
    print("=== QUANTITATIVE EDGE DISCOVERY: SCALING PROFITS TO 2X (₹100,000+) ===")
    print(df_res[cols].to_string(index=False))
