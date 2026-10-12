#!/usr/bin/env python3
"""
True Event-Driven Compounding Portfolio Simulation across 2024-2026.
- Starts with ₹100,000.00 cash pool.
- Dynamically compounds: position size scales with current total account equity.
- Evaluates:
  1. Static All 49 Stocks vs Top Dynamic Leaders (Relative Strength)
  2. Fixed Capital (No Compounding) vs Full Compounding (Reinvesting Profits)
  3. 2 Concentrated Positions (50% each) vs 3 Positions (33% each)
- Strictly event-driven single shared cash pool, with Upstox delivery fees + STT + DP charges + slippage.
"""
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

def run_compounding_backtest(
    top_n_leaders=5,             # None for all stocks, 5 for Top 5 Leaders
    max_active_pos=2,            # 2 positions
    max_alloc_pct=0.50,          # 50% equity per position
    compound=True,               # If True, scales position size with equity
    start_year='2024',
    end_year='2026',
    target_mode='prior_high',
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in ALL_DATA]
    
    dates_set = set()
    for s in symbols:
        records, _, _ = ALL_DATA[s]
        for d in records.keys():
            if d >= f"{start_year}-01-01" and d <= f"{end_year}-12-31":
                dates_set.add(d)
    all_dates = sorted(list(dates_set))
    
    initial_capital = 100000.0
    cash = initial_capital
    open_positions = {}
    closed_trades = []
    
    # State tracking: last swing low/high
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
        
    equity_curve = []
    
    for current_date in all_dates:
        # 1. Manage Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            # Trail SL to BE when +1R is reached
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
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'Stock': sym,
                    'Bought': pos['entry_date'],
                    'Sold': current_date,
                    'Year': current_date[:4],
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': cost['gross'],
                    'Total Fees': cost['total'],
                    'Net': cost['net'],
                    'Result': exit_r,
                })
                del open_positions[sym]

        # 2. Dynamic Relative Strength Ranking
        sym_momentum = []
        for sym in symbols:
            records, df, date_to_idx = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
            idx = date_to_idx.get(current_date)
            if idx is not None and idx >= 15:
                chk = idx - 15
                if df.loc[chk, 's_low']:
                    state[sym]['l_val'] = df.loc[chk, 'low']
                    state[sym]['l_idx'] = chk
                if df.loc[chk, 's_high']:
                    state[sym]['h_val'] = df.loc[chk, 'high']
                    state[sym]['h_idx'] = chk
            
            # Leader qualification
            mom = r['mom60']
            ema50 = r['ema50']
            ema200 = r['ema200']
            close = r['close']
            if pd.notna(mom) and pd.notna(ema50) and pd.notna(ema200):
                # Uptrend qualification
                if close > ema50:
                    sym_momentum.append((sym, mom))
                    
        sym_momentum.sort(key=lambda x: x[1], reverse=True)
        if top_n_leaders is not None:
            active_eligible = set([s for s, m in sym_momentum[:top_n_leaders]])
        else:
            active_eligible = set([s for s, m in sym_momentum])

        # 3. Screen for Fibonacci Entries among Eligible Leaders
        candidates = []
        if len(open_positions) < max_active_pos:
            for sym in active_eligible:
                if sym in open_positions: continue
                records, df, date_to_idx = ALL_DATA[sym]
                r = records.get(current_date)
                if not r: continue
                
                s_info = state[sym]
                l_val, l_idx = s_info['l_val'], s_info['l_idx']
                h_val, h_idx = s_info['h_val'], s_info['h_idx']
                if l_val is None or h_val is None or h_idx <= l_idx: continue
                
                swing_size = h_val - l_val
                if swing_size / l_val < 0.06: continue
                
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                
                c_low = r['low']
                c_high = r['high']
                c_close = r['close']
                c_open = r['open']
                
                # Pullback into golden pocket
                if c_low <= fib_500 and c_high >= fib_618:
                    body = abs(c_close - c_open)
                    candle_range = c_high - c_low
                    lower_wick = min(c_open, c_close) - c_low
                    if candle_range > 0 and (lower_wick / candle_range >= 0.25 or c_close > c_open):
                        entry_p = c_close
                        atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                        sl = min(fib_618, c_low) - (0.5 * atr)
                        
                        if target_mode == 'prior_high':
                            tp = h_val
                        else:
                            tp = entry_p + 2.0 * (entry_p - sl)
                            
                        risk = entry_p - sl
                        reward = tp - entry_p
                        if risk > 0 and reward / risk >= 1.0:
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'tp': tp,
                                'date': current_date,
                            })

        # 4. Sizing with Dynamic Compounding
        port_equity = cash + sum(p['qty'] * ALL_DATA[s][0].get(current_date, {}).get('close', p['entry_p']) for s, p in open_positions.items())
        
        if compound:
            alloc_base = port_equity
        else:
            alloc_base = initial_capital
            
        max_alloc = alloc_base * max_alloc_pct
        for cand in candidates:
            if len(open_positions) >= max_active_pos: break
            qty = min(int(max_alloc / cand['entry_p']), int(cash / cand['entry_p']))
            if qty > 0:
                cost_estimate = qty * cand['entry_p']
                if cash >= cost_estimate:
                    cash -= cost_estimate
                    open_positions[cand['sym']] = {
                        'entry_p': cand['entry_p'],
                        'sl': cand['sl'],
                        'orig_sl': cand['sl'],
                        'tp': cand['tp'],
                        'qty': qty,
                        'entry_date': cand['date'],
                    }

        # Daily Equity Tracking
        current_eq = cash + sum(p['qty'] * ALL_DATA[s][0].get(current_date, {}).get('close', p['entry_p']) for s, p in open_positions.items())
        equity_curve.append((current_date, current_eq))

    # Mark to Market at end
    final_eq = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    wins = [t for t in closed_trades if t['Net'] > 0]
    losses = [t for t in closed_trades if t['Net'] <= 0]
    
    # Yearly stats
    df_trades = pd.DataFrame(closed_trades) if closed_trades else pd.DataFrame()
    yearly = {}
    if not df_trades.empty:
        for y, grp in df_trades.groupby('Year'):
            y_wins = sum(grp['Net'] > 0)
            yearly[y] = {
                'trades': len(grp),
                'wins': y_wins,
                'wr': round(y_wins / len(grp) * 100, 1),
                'gross': round(grp['Gross'].sum(), 1),
                'fees': round(grp['Total Fees'].sum(), 1),
                'net': round(grp['Net'].sum(), 1),
            }
            
    return {
        'initial_capital': initial_capital,
        'final_equity': round(final_eq, 2),
        'net_profit': round(final_eq - initial_capital, 2),
        'roi_pct': round((final_eq - initial_capital) / initial_capital * 100, 2),
        'total_trades': len(closed_trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
        'total_fees': round(sum(t['Total Fees'] for t in closed_trades), 2),
        'yearly': yearly,
        'closed_trades': closed_trades,
    }

if __name__ == '__main__':
    print("Running true single-portfolio compounding backtests...\n")
    
    # 1. 2026 Year alone: Non-Compounded vs Compounded
    res_2026_no_comp = run_compounding_backtest(start_year='2026', end_year='2026', compound=False, max_active_pos=2, max_alloc_pct=0.50, top_n_leaders=5)
    res_2026_comp = run_compounding_backtest(start_year='2026', end_year='2026', compound=True, max_active_pos=2, max_alloc_pct=0.50, top_n_leaders=5)
    
    print("=== 2026 PERFORMANCE (TOP 5 LEADERS, 2 CONCENTRATED POSITIONS) ===")
    print(f"Non-Compounded: Equity = ₹{res_2026_no_comp['final_equity']:,.2f} | Net Realized = ₹{sum(t['Net'] for t in res_2026_no_comp['closed_trades']):,.2f} | Trades = {res_2026_no_comp['total_trades']}")
    print(f"Compounded:     Equity = ₹{res_2026_comp['final_equity']:,.2f} | Net Realized = ₹{sum(t['Net'] for t in res_2026_comp['closed_trades']):,.2f} | Trades = {res_2026_comp['total_trades']}\n")
    
    # 2. Multi-Year Run (2024 - 2026): Non-Compounded vs Compounded
    print("=== MULTI-YEAR PERFORMANCE (2024 - 2026) ===")
    multi_no_comp = run_compounding_backtest(start_year='2024', end_year='2026', compound=False, max_active_pos=2, max_alloc_pct=0.50, top_n_leaders=5)
    multi_comp = run_compounding_backtest(start_year='2024', end_year='2026', compound=True, max_active_pos=2, max_alloc_pct=0.50, top_n_leaders=5)
    
    print(f"Without Compounding (Fixed ₹50k per pos): Ending Equity = ₹{multi_no_comp['final_equity']:,.2f} (+{multi_no_comp['roi_pct']}%) | Fees = ₹{multi_no_comp['total_fees']:,.2f}")
    print(f"WITH COMPOUNDING (Reinvesting Profits):    Ending Equity = ₹{multi_comp['final_equity']:,.2f} (+{multi_comp['roi_pct']}%) | Fees = ₹{multi_comp['total_fees']:,.2f}")
    print("\nYear-by-Year Breakdown (Compounded):")
    for y, d in multi_comp['yearly'].items():
        print(f"  Year {y}: {d['wins']}/{d['trades']} wins ({d['wr']}%) | Gross: ₹{d['gross']:,.1f} | Fees: ₹{d['fees']:,.1f} | Net: ₹{d['net']:,.1f}")
