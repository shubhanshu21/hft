const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** ₹1,23,456 (Indian digit grouping), optional explicit sign. */
export function rupees(v: number | null | undefined, opts: { sign?: boolean; decimals?: boolean } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  const body = (opts.decimals ? inr2 : inr).format(Math.abs(v));
  const sign = v < 0 ? '−' : opts.sign && v > 0 ? '+' : '';
  return `${sign}₹${body}`;
}
export function pct(v: number | null | undefined, digits = 1, sign = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  return `${v < 0 ? '−' : sign && v > 0 ? '+' : ''}${Math.abs(v).toFixed(digits)}%`;
}
export function num(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined || Number.isNaN(v) ? '–' : v.toFixed(digits);
}
export function tone(v: number | null | undefined): 'pos' | 'neg' | 'flat' {
  return !v ? 'flat' : v > 0 ? 'pos' : 'neg';
}
/** 2026-09-25T11:15:00+05:30 -> "25 Sep 11:15" (the time as recorded, no timezone conversion). */
export function shortTime(iso: string | null): string {
  if (!iso) return '–';
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${+m[3]} ${months[+m[2] - 1]} ${m[4]}:${m[5]}`;
}
export function shortDate(d: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d);
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return m ? `${+m[3]} ${months[+m[2] - 1]}` : d;
}
export function reasonLabel(r: string): string {
  return ({ be_stop: 'Breakeven stop', initial_stop: 'Stop loss', trail_stop: 'Trailing stop', take_profit: 'Take profit',
    timeout_exit: 'Timeout', eod_squareoff: 'End-of-day exit' } as Record<string, string>)[r] ?? r.replace(/_/g, ' ');
}
