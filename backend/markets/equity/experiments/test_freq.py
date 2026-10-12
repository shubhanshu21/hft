import os
from markets.equity.experiments.fibonacci_retracement import run_fibonacci_backtest, get_symbol_file, ARCHIVE_DIR

all_symbols = sorted(list(set(f.split('_')[0] for f in os.listdir(ARCHIVE_DIR) if '_' in f)))

trades_49_daily = 0
for sym in all_symbols:
    f = get_symbol_file(sym, 'daily')
    if not f: continue
    stats, trades = run_fibonacci_backtest(f, sym, 'daily', initial_capital=100000.0, risk_pct=5.0, target_type='rr2')
    trades_49_daily += sum(1 for t in trades if str(t.exit_time)[:4] == '2026')

print(f'2026 Trades on ALL 49 Stocks (Daily): {trades_49_daily} trades')
