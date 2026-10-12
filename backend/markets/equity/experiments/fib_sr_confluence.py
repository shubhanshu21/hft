#!/usr/bin/env python3
"""
Fibonacci + Support & Resistance (S/R Flip) Confluence Strategy.

Rules based on institutional trading principles:
1. Identify major upward impulse leg (Swing Low to Swing High, >= 6%).
2. Fibonacci Retracement into the 50.0% - 61.8% Golden Pocket.
3. S/R Confluence: Check if a prior Swing High (broken resistance acting as new support)
   or prior Swing Low cluster overlaps with the Fibonacci zone (within +/- 1.5%).
4. Moving Average Confluence: 50 EMA or 200 EMA near the Fibonacci zone.
5. Trigger: Bullish rejection candle (lower wick, close in upper 50%).
6. Target: Prior Swing High (Target 1) or 2:1 R:R.
7. Risk Management: Move SL to Breakeven at +1R.
8. Real Single-Account Portfolio Constraints: ₹100k capital, max 3 positions.
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
        
        # Precompute swings (lookback 10 and 15)
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


def run_fib_sr_simulation(
    require_sr_confluence=True,
    require_ema_confluence=False,
    target_type='prior_high',    # 'prior_high', 'rr2', 'rr1.5'
    trail_be_at_1r=True,
    max_active_pos=3,
    max_alloc_pct=0.33,
    min_swing_pct=0.06,
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
    
    # State tracking: list of past swing points
    state = {}
    for sym in symbols:
        records, df, _ = ALL_DATA[sym]
        lookback = 15
        df_pre = df[df['date_str'] < f"{start_year}-01-01"]
        past_highs = []
        past_lows = []
        for i in range(lookback * 2, len(df_pre)):
            chk = i - lookback
            if df.loc[chk, 's_low']:
                past_lows.append((chk, df.loc[chk, 'low']))
            if df.loc[chk, 's_high']:
                past_highs.append((chk, df.loc[chk, 'high']))
        state[sym] = {'past_highs': past_highs, 'past_lows': past_lows}

    for current_date in all_dates:
        # 1. Manage Exits
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            records, _, _ = ALL_DATA[sym]
            r = records.get(current_date)
            if not r: continue
            c_high = r['high']
            c_low = r['low']
            
            # Trail to breakeven
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
                    'Fees': cost['total'],
                    'Net': cost['net'],
                    'Result': exit_r,
                })
                del open_positions[sym]

        # 2. Entries
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
                        state[sym]['past_lows'].append((chk, df.loc[chk, 'low']))
                    if df.loc[chk, 's_high']:
                        state[sym]['past_highs'].append((chk, df.loc[chk, 'high']))
                        
            if sym in open_positions: continue
            
            past_h = state[sym]['past_highs']
            past_l = state[sym]['past_lows']
            if not past_h or not past_l: continue
            
            last_l_idx, last_l_val = past_l[-1]
            last_h_idx, last_h_val = past_h[-1]
            
            # Need upward impulse leg (Low followed by High)
            if last_h_idx > last_l_idx:
                s_range = last_h_val - last_l_val
                impulse_pct = s_range / last_l_val
                
                c_close = r['close']
                c_high = r['high']
                c_low = r['low']
                c_open = r['open']
                c_atr = r['atr'] if pd.notna(r['atr']) else c_close * 0.01
                c_ema50 = r['ema50'] if pd.notna(r['ema50']) else c_close
                c_ema20 = r['ema20'] if pd.notna(r['ema20']) else c_close
                
                if impulse_pct >= min_swing_pct and c_close >= c_ema50:
                    fib_50 = last_h_val - 0.500 * s_range
                    fib_618 = last_h_val - 0.618 * s_range
                    fib_786 = last_h_val - 0.786 * s_range
                    
                    in_zone = (c_low <= fib_50) and (c_low >= fib_786)
                    bar_rng = max(c_high - c_low, 1e-4)
                    bull_rejection = (c_close > c_open) and (c_close > fib_618) and ((c_close - c_low) / bar_rng >= 0.40)
                    
                    if in_zone and bull_rejection:
                        # --- SUPPORT & RESISTANCE CONFLUENCE CHECK ---
                        # Check if an earlier broken swing high or swing low sits inside the Fib zone
                        sr_confluence = False
                        if require_sr_confluence:
                            # Prior highs before the current impulse high
                            for p_idx, p_val in past_h[:-1][-5:]:
                                # If prior resistance falls within 2% of the Fib 50%-61.8% zone (Role Reversal)
                                if (fib_786 * 0.98 <= p_val <= fib_50 * 1.02):
                                    sr_confluence = True
                                    break
                            # Also check prior lows in the cluster
                            if not sr_confluence:
                                for p_idx, p_val in past_l[:-1][-5:]:
                                    if (fib_786 * 0.98 <= p_val <= fib_50 * 1.02):
                                        sr_confluence = True
                                        break
                        else:
                            sr_confluence = True
                            
                        # Optional: EMA confluence (EMA 50 or EMA 20 aligns with Fib zone)
                        ema_confluence = True
                        if require_ema_confluence:
                            ema_confluence = (fib_786 <= c_ema50 <= fib_50 * 1.02) or (fib_786 <= c_ema20 <= fib_50 * 1.02)
                            
                        if sr_confluence and ema_confluence:
                            entry_p = c_close
                            sl = round(last_l_val - 0.2 * c_atr, 2)
                            s_dist = entry_p - sl
                            if s_dist > 0:
                                if target_type == 'prior_high':
                                    tp = round(last_h_val, 2)
                                elif target_type == 'rr1.5':
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
                                    'date': current_date,
                                })

        # Sizing & Allocation (Max 3 positions, 33% each)
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
        'fees': round(sum(t['Fees'] for t in closed_trades), 1),
        'unrealized': round(unreal_total, 1),
        'final_equity': round(final_equity, 1),
        'roi_pct': round((final_equity - 100000.0) / 1000.0, 2),
        'closed_trades': closed_trades,
        'open_positions': unreal_list,
    }


if __name__ == '__main__':
    experiments = [
        ("1. Standard Fib without S/R (Baseline)", dict(require_sr_confluence=False, target_type='prior_high')),
        ("2. Fib + S/R Role Reversal Confluence (Prior High/Low in Zone)", dict(require_sr_confluence=True, target_type='prior_high')),
        ("3. Fib + S/R Confluence + 2.0:1 RR Target", dict(require_sr_confluence=True, target_type='rr2')),
        ("4. Fib + S/R Confluence + 1.5:1 RR Target", dict(require_sr_confluence=True, target_type='rr1.5')),
        ("5. Fib + S/R Confluence + EMA Confluence", dict(require_sr_confluence=True, require_ema_confluence=True, target_type='prior_high')),
        ("6. Fib + S/R Confluence (2 Concentrated Positions, 50% each)", dict(require_sr_confluence=True, target_type='prior_high', max_active_pos=2, max_alloc_pct=0.50)),
    ]
    
    rows = []
    for name, params in experiments:
        res = run_fib_sr_simulation(**params)
        res_summary = {
            'Setup': name,
            'Closed': res['closed'],
            'Open': res['open'],
            'Win Rate': f"{res['wr']}%",
            'Realized (₹)': f"₹{res['realized']:,.1f}",
            'Fees (₹)': f"₹{res['fees']:,.1f}",
            'Unrealized (₹)': f"₹{res['unrealized']:,.1f}",
            'Ending Equity (₹)': f"₹{res['final_equity']:,.1f}",
            'ROI (%)': f"{res['roi_pct']:+.2f}%",
        }
        rows.append(res_summary)
        
    df = pd.DataFrame(rows)
    print("=== FIBONACCI + SUPPORT/RESISTANCE CONFLUENCE: 2026 AUDIT (₹100,000 CAPITAL) ===\n")
    print(df.to_string(index=False))
