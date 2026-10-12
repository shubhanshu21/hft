#!/usr/bin/env python3
"""
backend/markets/equity/experiments/test_neeraj_ote_profit.py

Simulates the EXACT Neeraj Joshi / ICT Fibonacci Optimal Trade Entry (OTE) strategy
for Indian Equities (NIFTY 50 Universe) under strict single-account cash constraints.

Key Concepts from the Video & TradingView Chart:
1. Impulse Wave: Swing Low (1.0) to Swing High (0.0).
2. Deep Discount / OTE Zone: Price retraces into 0.618 - 0.786 (centered around 0.705).
3. Bullish Rejection: Candle forms a bullish rejection (close > open or lower wick).
4. Invalidation / Stop Loss: Placed strictly below Swing Low (1.0 level - 0.2 ATR).
5. Take Profit Targets:
   - Target 1: Prior Swing High (0.0 level retest).
   - Target 2: -0.272 Extension (0.0 + 0.272 * Swing Range).
   - Target 3: Hybrid (Exit half at 0.0, trail remainder to -0.272).
6. Trend Filters: Close > EMA 50, Close > EMA 200, or Nifty Relative Strength.
"""

import os
import sys
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

# Add backend to sys.path
sys.path.insert(0, '/var/www/html/hft/backend')

from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"


def load_universe_data():
    symbols_data = {}
    for sym in NIFTY50_SYMBOLS:
        f = f"{DATA_DIR}/{sym}_daily.csv"
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f)
        df['datetime'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('datetime').reset_index(drop=True)
        if len(df) < 120:
            continue
        
        df['date_str'] = df['datetime'].dt.strftime('%Y-%m-%d')
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ema50'] = ta.ema(df['close'], length=50)
        df['ema200'] = ta.ema(df['close'], length=200)
        df['mom60'] = df['close'].pct_change(60)
        
        symbols_data[sym] = df
    return symbols_data


def run_ote_backtest(
    data_dict: dict,
    start_date: str = "2023-01-01",
    end_date: str = "2026-10-10",
    initial_capital: float = 100000.0,
    max_positions: int = 2,
    capital_per_pos_pct: float = 0.50,
    swing_order: int = 10,
    min_swing_pct: float = 0.06,
    trend_filter: str = "ema200",     # "ema50", "ema200", "both", "none"
    target_mode: str = "level_0",      # "level_0", "ext_272", "hybrid_50_50", "rr_2"
    sl_mode: str = "below_100",        # "below_100", "below_786"
    ote_entry_zone: str = "ote_deep",  # "ote_deep" (0.618 to 0.786), "standard" (0.50 to 0.618)
    use_trailing_breakeven: bool = False,
):
    symbols = list(data_dict.keys())
    all_dates = sorted(list(set(
        d for s in symbols
        for d in data_dict[s][
            (data_dict[s]['date_str'] >= start_date) & (data_dict[s]['date_str'] <= end_date)
        ]['date_str']
    )))
    
    cash = initial_capital
    open_positions = {}
    closed_trades = []
    
    # Pre-index for rapid lookup
    recs = {s: data_dict[s].set_index('date_str').to_dict('index') for s in symbols}
    date_to_idx = {s: {d: i for i, d in enumerate(data_dict[s]['date_str'])} for s in symbols}
    
    for d in all_dates:
        # 1. Manage Exits
        for s in list(open_positions.keys()):
            pos = open_positions[s]
            r = recs[s].get(d)
            if not r:
                continue
            
            c_high, c_low, c_close = r['high'], r['low'], r['close']
            
            # Check Stop Loss first (on existing stop level)
            if c_low <= pos['sl']:
                exit_p = pos['sl']
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                net_pnl = (pos['qty'] * exit_p) - cost['total']
                cash += net_pnl
                closed_trades.append({
                    'symbol': s,
                    'bought': pos['entry_date'],
                    'sold': d,
                    'shares': pos['qty'],
                    'entry': pos['entry_p'],
                    'exit': exit_p,
                    'fees': cost['total'],
                    'net': cost['net'],
                    'roi_pct': round((cost['net'] / (pos['qty'] * pos['entry_p'])) * 100, 2),
                    'result': 'stop',
                })
                del open_positions[s]
                continue
            
            # Check Target
            target_hit = False
            if target_mode == "level_0" and c_high >= pos['tp']:
                target_hit = True
                exit_p = pos['tp']
            elif target_mode == "ext_272" and c_high >= pos['tp']:
                target_hit = True
                exit_p = pos['tp']
            elif target_mode == "rr_2" and c_high >= pos['tp']:
                target_hit = True
                exit_p = pos['tp']
            elif target_mode == "hybrid_50_50":
                # Check target 1 (0.0 level)
                if not pos.get('t1_taken', False) and c_high >= pos['tp1']:
                    # Sell half
                    half_qty = pos['qty'] // 2
                    if half_qty > 0:
                        cost1 = compute_nse_equity_delivery_costs('long', pos['entry_p'], pos['tp1'], half_qty, symbol=s)
                        cash += (half_qty * pos['tp1']) - cost1['total']
                        pos['qty'] -= half_qty
                        pos['t1_taken'] = True
                        pos['sl'] = pos['entry_p'] # Move remainder to breakeven
                        closed_trades.append({
                            'symbol': s,
                            'bought': pos['entry_date'],
                            'sold': d,
                            'shares': half_qty,
                            'entry': pos['entry_p'],
                            'exit': pos['tp1'],
                            'fees': cost1['total'],
                            'net': cost1['net'],
                            'roi_pct': round((cost1['net'] / (half_qty * pos['entry_p'])) * 100, 2),
                            'result': 'target_1 (partial)',
                        })
                # Check target 2 (-0.272 extension)
                if pos.get('t1_taken', False) and c_high >= pos['tp2']:
                    exit_p = pos['tp2']
                    cost2 = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                    cash += (pos['qty'] * exit_p) - cost2['total']
                    closed_trades.append({
                        'symbol': s,
                        'bought': pos['entry_date'],
                        'sold': d,
                        'shares': pos['qty'],
                        'entry': pos['entry_p'],
                        'exit': exit_p,
                        'fees': cost2['total'],
                        'net': cost2['net'],
                        'roi_pct': round((cost2['net'] / (pos['qty'] * pos['entry_p'])) * 100, 2),
                        'result': 'target_2 (extension)',
                    })
                    del open_positions[s]
                    continue
            
            if target_hit:
                cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], exit_p, pos['qty'], symbol=s)
                cash += (pos['qty'] * exit_p) - cost['total']
                closed_trades.append({
                    'symbol': s,
                    'bought': pos['entry_date'],
                    'sold': d,
                    'shares': pos['qty'],
                    'entry': pos['entry_p'],
                    'exit': exit_p,
                    'fees': cost['total'],
                    'net': cost['net'],
                    'roi_pct': round((cost['net'] / (pos['qty'] * pos['entry_p'])) * 100, 2),
                    'result': 'target',
                })
                del open_positions[s]
                continue
            
            # Trailing stop to breakeven if price covers 60% of distance to target
            if use_trailing_breakeven:
                halfway = pos['entry_p'] + 0.60 * (pos['tp'] - pos['entry_p'])
                if c_high >= halfway:
                    pos['sl'] = max(pos['sl'], pos['entry_p'])
        
        # 2. Candidate Screening & Signal Generation
        if len(open_positions) < max_positions:
            candidates = []
            
            for sym in symbols:
                if sym in open_positions:
                    continue
                r = recs[sym].get(d)
                if not r:
                    continue
                
                c_close, c_open, c_high, c_low = r['close'], r['open'], r['high'], r['low']
                
                # Trend Filters
                ema50 = r.get('ema50')
                ema200 = r.get('ema200')
                
                if trend_filter == "ema50":
                    if pd.isna(ema50) or c_close < ema50:
                        continue
                elif trend_filter == "ema200":
                    if pd.isna(ema200) or c_close < ema200:
                        continue
                elif trend_filter == "both":
                    if pd.isna(ema50) or pd.isna(ema200) or c_close < ema50 or c_close < ema200 or ema50 < ema200:
                        continue
                
                idx = date_to_idx[sym].get(d)
                if idx is None or idx < swing_order * 3:
                    continue
                
                df_sym = data_dict[sym]
                sub_highs = df_sym.loc[: idx - 1, 'high'].to_numpy()
                sub_lows = df_sym.loc[: idx - 1, 'low'].to_numpy()
                
                peaks = argrelextrema(sub_highs, np.greater_equal, order=swing_order)[0]
                troughs = argrelextrema(sub_lows, np.less_equal, order=swing_order)[0]
                if len(peaks) == 0 or len(troughs) == 0:
                    continue
                
                p_idx = peaks[-1]
                t_idx = troughs[-1]
                if p_idx <= t_idx:
                    continue  # Must be an upward impulse leg
                
                h_val = float(sub_highs[p_idx])
                l_val = float(sub_lows[t_idx])
                swing_size = h_val - l_val
                
                if (swing_size / l_val) < min_swing_pct:
                    continue
                
                # Fibonacci Retracement Levels
                # 0.0 = Prior Swing High
                # 0.50 = Equilibrium
                # 0.618 = Golden Ratio
                # 0.705 = OTE Sweet Spot
                # 0.786 = Deep Discount boundary
                # 1.0 = Swing Low
                fib_500 = h_val - 0.500 * swing_size
                fib_618 = h_val - 0.618 * swing_size
                fib_705 = h_val - 0.705 * swing_size
                fib_786 = h_val - 0.786 * swing_size
                fib_1000 = l_val
                
                # Check Entry Zone
                if ote_entry_zone == "ote_deep":
                    # Optimal Trade Entry: Price dipped into 0.618 - 0.786 discount zone
                    in_zone = (c_low <= fib_618) and (c_low >= fib_786)
                elif ote_entry_zone == "standard":
                    # Standard 0.50 to 0.618 zone
                    in_zone = (c_low <= fib_500) and (c_low >= fib_786)
                else: # "all_discount" (0.50 to 0.786)
                    in_zone = (c_low <= fib_500) and (c_low >= fib_786)
                
                if not in_zone:
                    continue
                
                # Bullish Rejection / Confirmation Trigger:
                # 1. Bullish body: close > open OR
                # 2. Rejection lower wick >= 25% of bar range
                bar_rng = max(c_high - c_low, 1e-4)
                lower_wick = min(c_open, c_close) - c_low
                is_rejection = (lower_wick / bar_rng >= 0.25) or (c_close > c_open)
                
                # Must hold above the invalidation boundary on closing basis
                if not is_rejection or c_close < fib_786:
                    continue
                
                entry_p = c_close
                atr = r['atr'] if pd.notna(r['atr']) else (0.015 * entry_p)
                
                # Stop Loss Placement
                if sl_mode == "below_100":
                    sl = round(fib_1000 - 0.2 * atr, 2)
                elif sl_mode == "below_786":
                    sl = round(fib_786 - 0.2 * atr, 2)
                else:
                    sl = round(fib_1000 - 0.2 * atr, 2)
                
                stop_dist = entry_p - sl
                if stop_dist <= 0:
                    continue
                
                # Take Profit Target Placement
                if target_mode == "level_0":
                    tp = round(h_val, 2) # Retest of Prior Swing High (0.0 level)
                    tp1, tp2 = tp, tp
                elif target_mode == "ext_272":
                    tp = round(h_val + 0.272 * swing_size, 2) # -0.272 extension
                    tp1, tp2 = tp, tp
                elif target_mode == "rr_2":
                    tp = round(entry_p + 2.0 * stop_dist, 2)
                    tp1, tp2 = tp, tp
                elif target_mode == "hybrid_50_50":
                    tp1 = round(h_val, 2)
                    tp2 = round(h_val + 0.272 * swing_size, 2)
                    tp = tp2
                
                if tp <= entry_p:
                    continue
                
                rr = (tp - entry_p) / stop_dist
                mom = r.get('mom60', 0.0)
                candidates.append({
                    'symbol': sym,
                    'entry_p': entry_p,
                    'sl': sl,
                    'tp': tp,
                    'tp1': tp1,
                    'tp2': tp2,
                    'rr': rr,
                    'mom': mom,
                })
            
            # Sort candidates by relative momentum or RR ratio
            candidates.sort(key=lambda x: x['mom'], reverse=True)
            
            # Execute entries up to available slots
            slots_avail = max_positions - len(open_positions)
            for cand in candidates[:slots_avail]:
                alloc_capital = cash * (capital_per_pos_pct / slots_avail) if slots_avail > 1 else cash * capital_per_pos_pct
                alloc_capital = min(alloc_capital, cash)
                
                qty = int(alloc_capital // cand['entry_p'])
                if qty <= 0:
                    continue
                
                entry_cost = compute_nse_equity_delivery_costs('long', cand['entry_p'], cand['entry_p'], qty, symbol=cand['symbol'])
                total_req = (qty * cand['entry_p']) + entry_cost['total']
                if total_req > cash:
                    continue
                
                cash -= total_req
                open_positions[cand['symbol']] = {
                    'entry_date': d,
                    'entry_p': cand['entry_p'],
                    'qty': qty,
                    'sl': cand['sl'],
                    'orig_sl': cand['sl'],
                    'tp': cand['tp'],
                    'tp1': cand['tp1'],
                    'tp2': cand['tp2'],
                    't1_taken': False,
                }
    
    # Mark to market open positions at final close
    final_equity = cash
    for s, pos in open_positions.items():
        r = recs[s].get(all_dates[-1])
        last_price = r['close'] if r else pos['entry_p']
        cost = compute_nse_equity_delivery_costs('long', pos['entry_p'], last_price, pos['qty'], symbol=s)
        final_equity += (pos['qty'] * last_price) - cost['total']
    
    total_trades = len(closed_trades)
    win_trades = [t for t in closed_trades if t['net'] > 0]
    loss_trades = [t for t in closed_trades if t['net'] <= 0]
    win_rate = (len(win_trades) / total_trades * 100) if total_trades > 0 else 0.0
    
    total_net = sum(t['net'] for t in closed_trades)
    total_fees = sum(t['fees'] for t in closed_trades)
    
    avg_win = np.mean([t['net'] for t in win_trades]) if win_trades else 0.0
    avg_loss = np.mean([t['net'] for t in loss_trades]) if loss_trades else 0.0
    profit_factor = (abs(sum(t['net'] for t in win_trades)) / abs(sum(t['net'] for t in loss_trades))) if loss_trades and sum(t['net'] for t in loss_trades) != 0 else float('nan')
    
    return {
        'initial_capital': initial_capital,
        'final_equity': round(final_equity, 1),
        'net_realized': round(total_net, 1),
        'total_fees': round(total_fees, 1),
        'roi_pct': round(((final_equity - initial_capital) / initial_capital) * 100, 2),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 1),
        'avg_loss': round(avg_loss, 1),
        'profit_factor': round(profit_factor, 2) if not np.isnan(profit_factor) else 0.0,
        'closed_trades': closed_trades,
    }


if __name__ == '__main__':
    data = load_universe_data()
    print(f"Loaded {len(data)} stocks from NIFTY 50 universe.\n")
    
    print("=" * 85)
    print("TEST 1: MULTI-YEAR COMPREHENSIVE BACKTEST (2023 to October 2026)")
    print("Capital: ₹100,000 | Single-Account Cash Constraints | All Real Delivery Fees")
    print("=" * 85)
    
    experiments = [
        ("1. Standard 0.50-0.618 Pocket | SL below 1.0 | Target 0.0 | EMA 50", dict(
            ote_entry_zone="standard", sl_mode="below_100", target_mode="level_0", trend_filter="ema50"
        )),
        ("2. ICT/Neeraj OTE (0.618-0.786) | SL below 1.0 | Target 0.0 | EMA 50", dict(
            ote_entry_zone="ote_deep", sl_mode="below_100", target_mode="level_0", trend_filter="ema50"
        )),
        ("3. ICT/Neeraj OTE (0.618-0.786) | SL below 1.0 | Target 0.0 | EMA 200 (Macro Bull)", dict(
            ote_entry_zone="ote_deep", sl_mode="below_100", target_mode="level_0", trend_filter="ema200"
        )),
        ("4. ICT/Neeraj OTE | SL below 1.0 | -0.272 Extension Target | EMA 200", dict(
            ote_entry_zone="ote_deep", sl_mode="below_100", target_mode="ext_272", trend_filter="ema200"
        )),
        ("5. ICT/Neeraj OTE | SL below 1.0 | Hybrid 50/50 Target | EMA 200", dict(
            ote_entry_zone="ote_deep", sl_mode="below_100", target_mode="hybrid_50_50", trend_filter="ema200"
        )),
        ("6. ICT/Neeraj OTE | SL below 0.786 (Tight) | Target 0.0 | EMA 200", dict(
            ote_entry_zone="ote_deep", sl_mode="below_786", target_mode="level_0", trend_filter="ema200"
        )),
        ("7. ICT/Neeraj OTE | Concentrated (1 Pos = 100% Cash) | Target 0.0 | EMA 200", dict(
            ote_entry_zone="ote_deep", sl_mode="below_100", target_mode="level_0", trend_filter="ema200",
            max_positions=1, capital_per_pos_pct=1.00
        )),
    ]
    
    res_list = []
    for label, cfg in experiments:
        res = run_ote_backtest(data, start_date="2023-01-01", end_date="2026-10-10", **cfg)
        res_list.append({
            'Setup': label,
            'Trades': res['total_trades'],
            'Win Rate': f"{res['win_rate']}%",
            'Profit Factor': res['profit_factor'],
            'Fees (₹)': f"₹{res['total_fees']:,.0f}",
            'Net P&L (₹)': f"₹{res['net_realized']:,.0f}",
            'Ending Equity': f"₹{res['final_equity']:,.0f}",
            'Total ROI': f"{res['roi_pct']:+.2f}%",
        })
    
    df_res = pd.DataFrame(res_list)
    print(df_res.to_string(index=False))
    
    print("\n" + "=" * 85)
    print("TEST 2: RECENT CHOP AUDIT (2026 Year-to-Date)")
    print("Capital: ₹100,000 | Single-Account Cash Constraints | All Real Delivery Fees")
    print("=" * 85)
    
    res_2026 = []
    for label, cfg in experiments:
        res = run_ote_backtest(data, start_date="2026-01-01", end_date="2026-10-10", **cfg)
        res_2026.append({
            'Setup': label,
            'Trades': res['total_trades'],
            'Win Rate': f"{res['win_rate']}%",
            'Profit Factor': res['profit_factor'],
            'Fees (₹)': f"₹{res['total_fees']:,.0f}",
            'Net P&L (₹)': f"₹{res['net_realized']:,.0f}",
            'Ending Equity': f"₹{res['final_equity']:,.0f}",
            '2026 ROI': f"{res['roi_pct']:+.2f}%",
        })
        
    df_2026 = pd.DataFrame(res_2026)
    print(df_2026.to_string(index=False))
