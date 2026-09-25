export interface Kpis {
  trades: number; wins: number; losses: number; win_pct: number; need_pct: number;
  avg_win: number; avg_loss: number; payoff: number | null; profit_factor: number | null; expectancy: number;
  net: number; gross: number; costs: number; costs_pct_of_gross: number | null; best: number; worst: number;
  avg_hold_min: number; max_drawdown_pct: number;
}
export interface OpenPosition {
  symbol: string; direction: 'long' | 'short'; qty: number; entry_price: number; current_stop: number;
  target_price: number | null; entry_time: string; strategy: string;
}
export interface Overview {
  generated_at: string;
  account: { initial: number; current: number; net: number; return_pct: number; since: string };
  today: { date: string; trades: number; wins: number; net: number; win_pct: number };
  kpis: Kpis;
  open_positions: OpenPosition[];
}
export interface EquityPoint { t: string | null; equity: number; drawdown_pct: number }
export interface DailyRow { date: string; net: number; trades: number; wins: number }
export interface SymbolRow extends Omit<Kpis, 'max_drawdown_pct'> { symbol: string; series: number[] }
export interface ExitRow { reason: string; trades: number; net: number; avg: number }
export interface TradeRow {
  trade_id: number; symbol: string; direction: string; qty: number; entry_price: number; exit_price: number;
  entry_dt: string; exit_dt: string; hold_minutes: number; exit_reason: string; gross_pnl: number;
  total_friction: number; net_pnl: number; strategy: string;
}
export interface SystemInfo {
  generated_at: string;
  heartbeat: { phase: string; ts: string; problem: string | null } | null;
  token_hours_left: number | null; last_backup: string | null; trading_enabled: boolean;
  upstox_limits: { symbol: string; margin_per_lot: number; upstox_leverage: number; lot_size: number | null; tick_size: number | null; as_of: string }[];
  sessions: { market: string; open: string; close: string; last_entry: string; forced_exit: string }[];
}
