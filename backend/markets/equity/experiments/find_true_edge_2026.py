#!/usr/bin/env python3
"""
Rigorous Multi-Hypothesis Backtest Engine for 2026 Equity on a REAL Single ₹100,000 Account.
Tests:
1. Fixed Fibonacci with bug fixes (proper lookback, next-bar breakeven)
2. Retest Target (Prior High / 1R) vs 2R vs 2.5R
3. Trend Filter (EMA 50 / EMA 200) + Relative Strength
4. Mean Reversion Pullbacks (RSI < 35 Golden Pocket)
5. Multi-timeframe swings

Strict Realism:
- True single-account event-driven simulation (1 shared ₹100k cash pool).
- Position sizes never exceed available cash.
- Max 1 position per stock (no fake pyramiding).
- Full Upstox statutory costs (STT, DP charges, brokerage, GST, and slippage).
"""
import glob
import os
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"

def load_all_daily():
    data = {}
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_daily.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        if len(df) < 100: continue
        
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema20'] = ta.ema(df['close'], length=20)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['mom60'] = df['close'].pct_change(60)
        
        data[sym] = df
    return data

DATA = load_all_daily()

def run_portfolio_simulation(
    target_type='prior_high',   # 'prior_high', 'rr1.5', 'rr2.0', 'rr2.5', 'ema20'
    max_active_positions=2,
    risk_pct_per_trade=3.0,     # Risk % of current cash
    max_capital_per_pos=0.50,   # Max 50% capital per trade
    swing_lookback=15,
    min_swing_pct=0.05,
    require_rs_leader=True,     # Only top momentum leaders
    require_rsi_oversold=False, # RSI < 45 pullback
    trail_be_at_1r=True,
    start_year='2026',
):
    symbols = [s for s in NIFTY50_SYMBOLS if s in DATA]
    
    # Pre-index by date
    records = {}
    dates_set = set()
    for s in symbols:
        df = DATA[s]
        df_sub = df[df['date_str'] >= f"{start_year}-01-01"]
        for d in df_sub['date_str']:
            dates_set.add(d)
        records[s] = df.set_index('date_str').to_dict('index')
        
    all_dates = sorted(list(dates_set))
    
    cash = 100000.0
    open_positions = {}
    closed_trades = []
    
    # Precompute swing points per symbol up to each date
    swing_cache = {}
    for s in symbols:
        df = DATA[s]
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        n = len(df)
        peaks = argrelextrema(highs, np.greater_equal, order=swing_lookback)[0]
        troughs = argrelextrema(lows, np.less_equal, order=swing_lookback)[0]
        
        # Map each row to latest peak and trough before it
        p_map = {}
        t_map = {}
        curr_p = None
        curr_t = None
        p_idx = 0
        t_idx = 0
        for i in range(n):
            # Peak must be confirmed at i - swing_lookback
            while p_idx < len(peaks) and peaks[p_idx] <= (i - swing_lookback):
                curr_p = peaks[p_idx]
                p_idx += 1
            while t_idx < len(troughs) and troughs[t_idx] <= (i - swing_lookback):
                curr_t = troughs[t_idx]
                t_idx += 1
            p_map[df.loc[i, 'date_str']] = curr_p
            t_map[df.loc[i, 'date_str']] = curr_t
            
        swing_cache[s] = (p_map, t_map)
        
    for current_date in all_dates:
        # 1. Manage Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            r = records[sym].get(current_date)
            if not r: continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            exit_p = None
            exit_r = None
            
            # Check Stop Loss First (prevents same-bar dip bug)
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
                    'Fees': cost['total'],
                    'Net': cost['net'],
                    'Result': exit_r,
                })
                del open_positions[sym]
            else:
                # Arm / trail stop to breakeven for NEXT bar
                if trail_be_at_1r:
                    risk_unit = pos['entry_p'] - pos['orig_sl']
                    if risk_unit > 0 and c_high >= (pos['entry_p'] + risk_unit):
                        pos['sl'] = max(pos['sl'], pos['entry_p'])

        # 2. Candidate Selection
        if len(open_positions) < max_active_positions:
            candidates = []
            
            # Leader filtering
            eligible = symbols
            if require_rs_leader:
                moms = []
                for s in symbols:
                    r = records[s].get(current_date)
                    if r and pd.notna(r.get('mom60')) and pd.notna(r.get('ema50')):
                        if r['close'] > r['ema50']:
                            moms.append((s, r['mom60']))
                moms.sort(key=lambda x: x[1], reverse=True)
                eligible = [s for s, m in moms[:10]]
                
            for sym in eligible:
                if sym in open_positions: continue
                r = records[sym].get(current_date)
                if not r: continue
                
                c_close, c_open, c_high, c_low = r['close'], r['open'], r['high'], r['low']
                ema50 = r.get('ema50')
                if pd.isna(ema50) or c_close < ema50: continue
                
                if require_rsi_oversold:
                    rsi = r.get('rsi')
                    if pd.isna(rsi) or rsi > 50: continue
                    
                p_map, t_map = swing_cache[sym]
                p_idx = p_map.get(current_date)
                t_idx = t_map.get(current_date)
                if p_idx is None or t_idx is None or p_idx <= t_idx: continue
                
                df_sym = DATA[sym]
                h_val = float(df_sym.loc[p_idx, 'high'])
                l_val = float(df_sym.loc[t_idx, 'low'])
                
                swing_size = h_val - l_val
                if swing_size / l_val < min_swing_pct: continue
                
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                fib_786 = h_val - 0.786 * swing_size
                
                if c_low <= fib_500 and c_low >= fib_786:
                    bar_rng = max(c_high - c_low, 1e-4)
                    bull_rej = (c_close > c_open) and (c_close >= fib_618) and ((c_close - c_low) / bar_rng >= 0.40)
                    if bull_rej:
                        entry_p = c_close
                        atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                        sl = round(l_val - 0.2 * atr, 2)
                        stop_dist = entry_p - sl
                        if stop_dist <= 0: continue
                        
                        if target_type == 'prior_high':
                            tp = round(h_val, 2)
                        elif target_type == 'rr1.5':
                            tp = round(entry_p + 1.5 * stop_dist, 2)
                        elif target_type == 'rr2.0':
                            tp = round(entry_p + 2.0 * stop_dist, 2)
                        elif target_type == 'rr2.5':
                            tp = round(entry_p + 2.5 * stop_dist, 2)
                        elif target_type == 'ema20':
                            tp = round(max(r['ema20'], entry_p + 1.0 * stop_dist), 2)
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

            # 3. Position Sizing on REAL Cash Balance
            port_equity = cash + sum(p['qty'] * records[s].get(current_date, {}).get('close', p['entry_p']) for s, p in open_positions.items())
            for cand in candidates:
                if len(open_positions) >= max_active_positions: break
                
                # Risk-based sizing capped by cash & max allocation
                risk_cash = port_equity * (risk_pct_per_trade / 100.0)
                shares_by_risk = int(risk_cash / cand['stop_dist'])
                shares_by_cap = int((port_equity * max_capital_per_pos) / cand['entry_p'])
                shares_by_cash = int(cash / cand['entry_p'])
                
                qty = min(shares_by_risk, shares_by_cap, shares_by_cash)
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
                            'entry_date': current_date,
                        }

    # Final Totals
    final_equity = cash + sum(p['qty'] * DATA[s].iloc[-1]['close'] for s, p in open_positions.items())
    wins = [t for t in closed_trades if t['Net'] > 0]
    total_trades = len(closed_trades)
    wr = round(len(wins) / total_trades * 100, 1) if total_trades else 0
    net_realized = round(sum(t['Net'] for t in closed_trades), 1)
    fees = round(sum(t['Fees'] for t in closed_trades), 1)
    roi_pct = round((final_equity - 100000.0) / 1000.0, 2)
    
    return {
        'trades': total_trades,
        'wins': len(wins),
        'wr': wr,
        'net_realized': net_realized,
        'fees': fees,
        'final_equity': round(final_equity, 1),
        'roi_pct': roi_pct,
        'closed_trades': closed_trades,
    }

if __name__ == '__main__':
    grid = [
        ("1. Prior Swing High Target + Trail BE + Top 10 Leaders (3% Risk)", dict(target_type='prior_high', trail_be_at_1r=True, require_rs_leader=True, risk_pct_per_trade=3.0)),
        ("2. Prior Swing High Target + Trail BE + Top 10 Leaders (5% Risk)", dict(target_type='prior_high', trail_be_at_1r=True, require_rs_leader=True, risk_pct_per_trade=5.0)),
        ("3. Prior Swing High Target (NO Trail BE - Full Room to Breathe)", dict(target_type='prior_high', trail_be_at_1r=False, require_rs_leader=True, risk_pct_per_trade=3.0)),
        ("4. 1.5:1 RR Target + Trail BE + Top 10 Leaders", dict(target_type='rr1.5', trail_be_at_1r=True, require_rs_leader=True, risk_pct_per_trade=3.0)),
        ("5. 2.0:1 RR Target + Trail BE + Top 10 Leaders", dict(target_type='rr2.0', trail_be_at_1r=True, require_rs_leader=True, risk_pct_per_trade=3.0)),
        ("6. 2.5:1 RR Target + Trail BE + Top 10 Leaders", dict(target_type='rr2.5', trail_be_at_1r=True, require_rs_leader=True, risk_pct_per_trade=3.0)),
        ("7. Prior High + RSI < 50 Pullback Filter", dict(target_type='prior_high', require_rsi_oversold=True, trail_be_at_1r=True, risk_pct_per_trade=3.0)),
        ("8. All 49 Stocks + Prior High Target (No RS filter)", dict(target_type='prior_high', require_rs_leader=False, trail_be_at_1r=True, risk_pct_per_trade=3.0)),
    ]
    
    print("=== RIGOROUS REALISTIC 2026 EQUITY BACKTEST AUDIT (₹100,000 CASH ACCOUNT) ===\n")
    rows = []
    for name, params in grid:
        res = run_portfolio_simulation(**params)
        rows.append({
            'Setup': name,
            'Trades': res['trades'],
            'Win Rate': f"{res['wr']}%",
            'Net Realized (₹)': f"₹{res['net_realized']:,.1f}",
            'Fees (₹)': f"₹{res['fees']:,.1f}",
            'Ending Equity (₹)': f"₹{res['final_equity']:,.1f}",
            'ROI (%)': f"{res['roi_pct']:+.2f}%",
        })
        
    df_grid = pd.DataFrame(rows)
    print(df_grid.to_string(index=False))
