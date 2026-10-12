#!/usr/bin/env python3
"""
2026 Compounding Backtest for ₹100,000 Capital.
- Starts with ₹100,000.00 cash pool on Jan 1, 2026.
- Compounding: Every trade size is dynamically calculated based on the updated total equity.
  When equity increases after a win, the next position size automatically increases (reinvesting 100% profits).
  When equity dips after a stop-out, position size scales down to protect downside.
- Compares:
  1. Top 5 Dynamic Leaders (2 Positions, 50% capital each)
  2. Top 8 Dynamic Leaders (2 Positions, 50% capital each)
  3. Top 5 Dynamic Leaders (3 Positions, 33% capital each)
  4. Top 8 Dynamic Leaders (3 Positions, 33% capital each)
- Includes all Upstox delivery fees, STT, DP charges, GST, and slippage.
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

def run_2026_compounding(
    top_n_leaders=5,
    max_active_pos=2,
    max_alloc_pct=0.50,
    compound=True,
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in ALL_DATA]
    
    dates_set = set()
    for s in symbols:
        records, _, _ = ALL_DATA[s]
        for d in records.keys():
            if d.startswith('2026'):
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
        df_pre = df[df['date_str'] < '2026-01-01']
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
        # 1. Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            # Trail SL to BE at +1R
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
                
                # Portfolio equity after exit
                curr_eq = cash + sum(p['qty'] * ALL_DATA[s][0].get(current_date, {}).get('close', p['entry_p']) for s, p in open_positions.items() if s != sym)
                
                closed_trades.append({
                    'Stock': sym,
                    'Bought': pos['entry_date'],
                    'Sold': current_date,
                    'Allocated (₹)': round(pos['qty'] * pos['entry_p'], 1),
                    'Shares': pos['qty'],
                    'Entry': pos['entry_p'],
                    'Exit': exit_p,
                    'Gross': round(cost['gross'], 1),
                    'Brokerage': round(cost['brokerage'], 1),
                    'STT': round(cost['stt'], 1),
                    'Total Fees': round(cost['total'], 1),
                    'Net P&L': round(cost['net'], 1),
                    'Account Equity (₹)': round(curr_eq, 1),
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
            
            mom = r['mom60']
            ema50 = r['ema50']
            close = r['close']
            if pd.notna(mom) and pd.notna(ema50):
                if close > ema50:
                    sym_momentum.append((sym, mom))
                    
        sym_momentum.sort(key=lambda x: x[1], reverse=True)
        active_eligible = set([s for s, m in sym_momentum[:top_n_leaders]])

        # 3. Screen for Fibonacci Pullbacks
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
                
                if c_low <= fib_500 and c_high >= fib_618:
                    candle_range = c_high - c_low
                    lower_wick = min(c_open, c_close) - c_low
                    if candle_range > 0 and (lower_wick / candle_range >= 0.25 or c_close > c_open):
                        entry_p = c_close
                        atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                        sl = min(fib_618, c_low) - (0.5 * atr)
                        tp = h_val
                        if tp > entry_p:
                            candidates.append({
                                'sym': sym,
                                'entry_p': entry_p,
                                'sl': sl,
                                'tp': tp,
                                'date': current_date,
                            })

        # 4. Sizing with Dynamic Compounding
        port_equity = cash + sum(p['qty'] * ALL_DATA[s][0].get(current_date, {}).get('close', p['entry_p']) for s, p in open_positions.items())
        
        alloc_base = port_equity if compound else initial_capital
        max_alloc = alloc_base * max_alloc_pct
        
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
                        'entry_date': cand['date'],
                    }

    # Mark to market (9 Oct 2026)
    unreal_total = 0.0
    unreal_list = []
    for sym, pos in open_positions.items():
        last_p = ALL_DATA[sym][1].iloc[-1]['close']
        unreal_pnl = (last_p - pos['entry_p']) * pos['qty']
        unreal_total += unreal_pnl
        unreal_list.append({
            'Stock': sym,
            'Shares': pos['qty'],
            'Entry': pos['entry_p'],
            'Current Price': round(last_p, 1),
            'Allocated (₹)': round(pos['qty'] * pos['entry_p'], 1),
            'Unrealised (₹)': round(unreal_pnl, 1)
        })
        
    final_equity = cash + sum(p['qty'] * ALL_DATA[s][1].iloc[-1]['close'] for s, p in open_positions.items())
    wins = [t for t in closed_trades if t['Net P&L'] > 0]
    
    return {
        'initial_capital': initial_capital,
        'final_equity': round(final_equity, 1),
        'net_realized': round(sum(t['Net P&L'] for t in closed_trades), 1),
        'gross_profit': round(sum(t['Gross'] for t in closed_trades), 1),
        'total_fees': round(sum(t['Total Fees'] for t in closed_trades), 1),
        'unrealized': round(unreal_total, 1),
        'roi_pct': round((final_equity - initial_capital) / 1000.0, 2),
        'total_trades': len(closed_trades),
        'wins': len(wins),
        'wr': round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
        'closed_trades': closed_trades,
        'open_positions': unreal_list,
    }

if __name__ == '__main__':
    configs = [
        ("Top 5 Leaders (2 Concentrated Pos, 50% Alloc, Compounded)", 5, 2, 0.50, True),
        ("Top 5 Leaders (2 Concentrated Pos, 50% Alloc, Non-Compounded)", 5, 2, 0.50, False),
        ("Top 8 Leaders (2 Concentrated Pos, 50% Alloc, Compounded)", 8, 2, 0.50, True),
        ("Top 8 Leaders (2 Concentrated Pos, 50% Alloc, Non-Compounded)", 8, 2, 0.50, False),
        ("Top 5 Leaders (3 Positions, 33% Alloc, Compounded)", 5, 3, 0.33, True),
        ("Top 8 Leaders (3 Positions, 33% Alloc, Compounded)", 8, 3, 0.33, True),
    ]
    
    rows = []
    results = {}
    for name, leaders, pos, alloc, comp in configs:
        res = run_2026_compounding(top_n_leaders=leaders, max_active_pos=pos, max_alloc_pct=alloc, compound=comp)
        results[name] = res
        rows.append({
            'Configuration': name,
            'Closed': res['total_trades'],
            'Win Rate': f"{res['wr']}%",
            'Gross (₹)': f"₹{res['gross_profit']:,.1f}",
            'Fees (₹)': f"₹{res['total_fees']:,.1f}",
            'Net Realized (₹)': f"₹{res['net_realized']:,.1f}",
            'Unrealized (₹)': f"₹{res['unrealized']:,.1f}",
            'Final Equity (₹)': f"₹{res['final_equity']:,.1f}",
            'ROI (%)': f"{res['roi_pct']:+.2f}%",
        })
        
    df = pd.DataFrame(rows)
    print("=== 2026 COMPOUNDING AUDIT (STARTING CAPITAL: ₹100,000.00) ===\n")
    print(df.to_string(index=False))
    
    # Print the full itemized trade log for Top 5 Leaders
    top5_3pos_name = "Top 5 Leaders (3 Positions, 33% Alloc, Compounded)"
    top5_3pos_res = results[top5_3pos_name]
    print(f"\n\n=== ITEMIZED 2026 TRADE LOG WITH COMPOUNDING: {top5_3pos_name} ===")
    df_trades = pd.DataFrame(top5_3pos_res['closed_trades'])
    cols = ['Stock', 'Bought', 'Sold', 'Allocated (₹)', 'Shares', 'Entry', 'Exit', 'Gross', 'Total Fees', 'Net P&L', 'Account Equity (₹)', 'Result']
    print(df_trades[cols].to_string(index=False))
    
    print("\nStill Open Positions at Close (9 Oct 2026):")
    for op in top5_3pos_res['open_positions']:
        print(f"  {op['Stock']}: {op['Shares']} shares @ ₹{op['Entry']} | Allocated: ₹{op['Allocated (₹)']} | Unrealized: ₹{op['Unrealised (₹)']}")

    top5_2pos_name = "Top 5 Leaders (2 Concentrated Pos, 50% Alloc, Compounded)"
    top5_2pos_res = results[top5_2pos_name]
    print(f"\n\n=== ITEMIZED 2026 TRADE LOG WITH COMPOUNDING: {top5_2pos_name} ===")
    df_trades2 = pd.DataFrame(top5_2pos_res['closed_trades'])
    print(df_trades2[cols].to_string(index=False))
    
    print("\nStill Open Positions at Close (9 Oct 2026):")
    for op in top5_2pos_res['open_positions']:
        print(f"  {op['Stock']}: {op['Shares']} shares @ ₹{op['Entry']} | Allocated: ₹{op['Allocated (₹)']} | Unrealized: ₹{op['Unrealised (₹)']}")

