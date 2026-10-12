#!/usr/bin/env python3
"""
Dynamic Stock Selection + Fibonacci Retracement + Support/Resistance.

Strategy:
1. DYNAMIC STOCK SELECTION: Every month/week, dynamically rank all 49 NIFTY 50 stocks
   by Relative Strength (60-day momentum) and Macro Trend (Close > 50 EMA > 200 EMA).
   Only the Top 10 Relative Strength Leaders are eligible to trade at any given time.
2. FIBONACCI + S/R CONFLUENCE:
   When an active Leader pulls back into the 50.0% - 61.8% Golden Pocket
   with a bullish rejection candle at Support.
3. EXIT:
   Target = Prior Swing High (or 2:1 R:R) with Breakeven Stop at +1R.
4. PORTFOLIO:
   Single ₹100,000 account, max 3 positions (33% capital each), full Upstox fees & slippage.
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
        if len(df) < 65: continue
        
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        # 60-day momentum (Relative Strength proxy)
        df['mom60'] = df['close'].pct_change(60)
        
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


def run_dynamic_portfolio(
    top_n_leaders=8,             # Trade only the top 8 strongest leaders dynamically
    min_swing_pct=0.06,
    target_mode='prior_high',    # 'prior_high' or 'rr2'
    trail_be_at_1r=True,
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
    
    # State tracking
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
        # 1. Manage Exits
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
                    'Bought': pos['entry_date'],
                    'Sold': current_date,
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': cost['gross'],
                    'Brokerage': cost['brokerage'],
                    'STT': cost['stt'],
                    'Slippage': cost['slippage'],
                    'Total Fees': cost['total'],
                    'Net': cost['net'],
                    'Result': exit_r,
                })
                del open_positions[sym]

        # 2. DYNAMIC RELATIVE STRENGTH SELECTION
        # Rank all stocks by 60-day momentum on current_date that are in Macro Uptrend (Close > 50 EMA)
        sym_momentum = []
        for sym in symbols:
            records, df, date_to_idx = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
            # Update swings
            idx = date_to_idx.get(current_date)
            if idx is not None and idx >= 15:
                chk = idx - 15
                if df.loc[chk, 's_low']:
                    state[sym]['l_val'] = df.loc[chk, 'low']
                    state[sym]['l_idx'] = chk
                if df.loc[chk, 's_high']:
                    state[sym]['h_val'] = df.loc[chk, 'high']
                    state[sym]['h_idx'] = chk
                    
            if pd.notna(r['mom60']) and r['close'] >= r['ema50']:
                sym_momentum.append((sym, r['mom60']))
                
        # Sort by momentum descending
        sym_momentum.sort(key=lambda x: x[1], reverse=True)
        # Select current Top Leaders
        current_leaders = set([s for s, m in sym_momentum[:top_n_leaders]])

        # 3. Candidate Entries ONLY from Current Dynamic Leaders
        candidates = []
        for sym in current_leaders:
            if sym in open_positions: continue
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
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
                
                if impulse_pct >= min_swing_pct:
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
                            tp = round(h_val, 2) if target_mode == 'prior_high' else round(entry_p + s_dist * 2.0, 2)
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'orig_sl': sl,
                                'tp': tp,
                                's_dist': s_dist,
                                'date': current_date,
                            })

        # 4. Sizing & Allocating
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
                    'entry_date': cand['date'],
                }

    # Mark to market (9 Oct 2026)
    unreal_total = 0.0
    unreal_list = []
    for sym, pos in open_positions.items():
        _, df, _ = ALL_DATA[sym]
        last_p = df.iloc[-1]['close']
        unreal_pnl = (last_p - pos['entry_p']) * pos['qty']
        unreal_total += unreal_pnl
        unreal_list.append({
            'Stock': sym,
            'Shares': pos['qty'],
            'Entry': pos['entry_p'],
            'Current Price': last_p,
            'Unrealised': round(unreal_pnl, 1)
        })
        
    final_equity = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    wins = sum(1 for t in closed_trades if t['Net'] > 0)
    
    return {
        'closed': len(closed_trades),
        'open': len(open_positions),
        'wins': wins,
        'wr': round(wins / len(closed_trades) * 100, 1) if closed_trades else 0,
        'realized': round(sum(t['Net'] for t in closed_trades), 1),
        'fees': round(sum(t['Total Fees'] for t in closed_trades), 1),
        'unrealized': round(unreal_total, 1),
        'final_equity': round(final_equity, 1),
        'roi_pct': round((final_equity - 100000.0) / 1000.0, 2),
        'closed_trades': closed_trades,
        'open_positions': unreal_list,
    }


if __name__ == '__main__':
    grid = [
        {"name": "1. Top 5 Dynamic Leaders (Prior High Target)", "top_n_leaders": 5, "target_mode": 'prior_high'},
        {"name": "2. Top 8 Dynamic Leaders (Prior High Target)", "top_n_leaders": 8, "target_mode": 'prior_high'},
        {"name": "3. Top 10 Dynamic Leaders (Prior High Target)", "top_n_leaders": 10, "target_mode": 'prior_high'},
        {"name": "4. Top 8 Dynamic Leaders (2.0:1 RR Target)", "top_n_leaders": 8, "target_mode": 'rr2'},
        {"name": "5. Top 10 Dynamic Leaders (2.0:1 RR Target)", "top_n_leaders": 10, "target_mode": 'rr2'},
        {"name": "6. Top 8 Dynamic Leaders (2 Concentrated Pos, 50% each)", "top_n_leaders": 8, "target_mode": 'prior_high', "max_active_pos": 2, "max_alloc_pct": 0.50},
    ]
    
    rows = []
    for g in grid:
        params = dict(g)
        name = params.pop('name')
        res = run_dynamic_portfolio(**params)
        res_summary = {
            'Configuration': name,
            'Closed': res['closed'],
            'Open': res['open'],
            'Win Rate': f"{res['wr']}%",
            'Realized (₹)': f"₹{res['realized']:,.1f}",
            'Fees (₹)': f"₹{res['fees']:,.1f}",
            'Unrealized (₹)': f"₹{res['unrealized']:,.1f}",
            'Final Equity (₹)': f"₹{res['final_equity']:,.1f}",
            'ROI (%)': f"{res['roi_pct']:+.2f}%",
        }
        rows.append(res_summary)
        
    df = pd.DataFrame(rows)
    print("=== DYNAMIC LEADER SELECTION + FIBONACCI (2026 AUDIT) ===\n")
    print(df.to_string(index=False))

    print("\n\n=== ITEMIZED TRADES FOR TOP 5 DYNAMIC LEADERS ===")
    top5_res = run_dynamic_portfolio(top_n_leaders=5, target_mode='prior_high')
    if top5_res['closed_trades']:
        t_df = pd.DataFrame(top5_res['closed_trades'])
        cols = ['Stock', 'Bought', 'Sold', 'Shares', 'Entry', 'Exit', 'Gross', 'Total Fees', 'Net', 'Result']
        print(t_df[cols].to_string(index=False))
    print("\nStill Open Positions:")
    for op in top5_res['open_positions']:
        print(op)

    print("\n\n=== ITEMIZED TRADES FOR TOP 8 DYNAMIC LEADERS ===")
    top8_res = run_dynamic_portfolio(top_n_leaders=8, target_mode='prior_high')
    if top8_res['closed_trades']:
        t_df = pd.DataFrame(top8_res['closed_trades'])
        cols = ['Stock', 'Bought', 'Sold', 'Shares', 'Entry', 'Exit', 'Gross', 'Total Fees', 'Net', 'Result']
        print(t_df[cols].to_string(index=False))
    print("\nStill Open Positions:")
    for op in top8_res['open_positions']:
        print(op)


