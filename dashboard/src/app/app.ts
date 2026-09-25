import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { Bar, BarChart } from './charts/bar-chart';
import { Donut, Slice } from './charts/donut';
import { LineChart, LinePoint } from './charts/line-chart';
import { Sparkline } from './charts/sparkline';
import { num, pct, reasonLabel, rupees, shortDate, shortTime, tone } from './format';

type View = 'overview' | 'symbols' | 'trades' | 'system';
const REASON_COLORS: Record<string, string> = {
  be_stop: '#29d391', trail_stop: '#4aa3ff', take_profit: '#a05cff', initial_stop: '#ff5d73', timeout_exit: '#f5b942', eod_squareoff: '#8a92a6',
};

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LineChart, BarChart, Sparkline, Donut],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  readonly api = inject(ApiService);
  readonly view = signal<View>(App.fromHash());
  readonly symbolFilter = signal<string>('ALL');
  readonly reasonFilter = signal<string>('ALL');
  readonly statusFilter = signal<'ALL' | 'OPEN' | 'CLOSED'>('ALL');
  readonly equityMode = signal<'equity' | 'drawdown'>('equity');

  readonly nav: { id: View; label: string; icon: string }[] = [
    { id: 'overview', label: 'Dashboard', icon: '◧' }, { id: 'symbols', label: 'Symbols', icon: '◫' },
    { id: 'trades', label: 'Trades', icon: '☰' }, { id: 'system', label: 'System', icon: '◎' },
  ];

  // formatters exposed to the template
  readonly rupees = rupees; readonly pct = pct; readonly num = num; readonly tone = tone;
  readonly shortTime = shortTime; readonly reasonLabel = reasonLabel;
  readonly money = (v: number) => rupees(v, { sign: true });
  readonly moneyPlain = (v: number) => rupees(v);
  readonly pctFmt = (v: number) => pct(v, 1);

  readonly o = this.api.overview;

  readonly equityPoints = computed<LinePoint[]>(() => {
    const mode = this.equityMode();
    return this.api.equity().map((p, i) => ({ label: p.t ? shortTime(p.t) : 'Start', y: mode === 'equity' ? p.equity : -p.drawdown_pct }));
  });
  readonly dailyBars = computed<Bar[]>(() => this.api.daily().map(d => ({ label: shortDate(d.date), value: d.net, sub: `${d.trades} trades · ${d.wins} wins` })));
  readonly slices = computed<Slice[]>(() => this.api.exits().map(e => ({ label: reasonLabel(e.reason), value: e.trades, color: REASON_COLORS[e.reason] ?? '#6b7391' })));
  readonly symbolNames = computed(() => ['ALL', ...this.api.symbols().map(s => s.symbol)]);
  readonly reasonNames = computed(() => ['ALL', ...this.api.exits().map(e => e.reason)]);
  readonly filteredTrades = computed(() => this.statusFilter() === 'OPEN' ? [] : this.api.trades().filter(t =>
    (this.symbolFilter() === 'ALL' || t.symbol === this.symbolFilter()) && (this.reasonFilter() === 'ALL' || t.exit_reason === this.reasonFilter())));
  /** Trades still open (the daemon manages them): shown above the closed ones unless the filter is 'Closed' or an exit reason is chosen. */
  readonly openTrades = computed(() => this.statusFilter() === 'CLOSED' || this.reasonFilter() !== 'ALL' ? [] :
    (this.api.overview()?.open_positions ?? []).filter(p => this.symbolFilter() === 'ALL' || p.symbol === this.symbolFilter()));
  readonly openTotals = computed(() => {
    const o = this.openTrades();
    return { n: o.length, unrealised: o.some(p => p.unrealised !== null) ? o.reduce((a, p) => a + (p.unrealised ?? 0), 0) : null };
  });
  readonly tradesTotals = computed(() => {
    const t = this.filteredTrades();
    return { n: t.length, net: t.reduce((a, r) => a + r.net_pnl, 0), fees: t.reduce((a, r) => a + r.total_friction, 0) };
  });
  readonly health = computed(() => {
    const s = this.api.system();
    if (!s) return { label: 'Unknown', cls: 'flat' };
    if (!s.heartbeat) return { label: 'No heartbeat', cls: 'neg' };
    if (s.heartbeat.problem) return { label: 'Not scanning', cls: 'neg' };
    return s.trading_enabled ? { label: 'Running', cls: 'pos' } : { label: 'Trading halted', cls: 'warn' };
  });
  readonly kpiCards = computed(() => {
    const k = this.o()?.kpis;
    if (!k) return [];
    return [
      { label: 'Win rate', value: pct(k.win_pct), hint: `needs ${pct(k.need_pct)} to break even`, cls: k.win_pct >= k.need_pct ? 'pos' : 'neg' },
      { label: 'Profit factor', value: num(k.profit_factor), hint: 'gross wins ÷ gross losses', cls: (k.profit_factor ?? 0) >= 1 ? 'pos' : 'neg' },
      { label: 'Avg win / avg loss', value: `${rupees(k.avg_win)} / ${rupees(k.avg_loss)}`, hint: `payoff ${num(k.payoff)}×`, cls: 'flat' },
      { label: 'Expectancy / trade', value: rupees(k.expectancy, { sign: true }), hint: 'net of all costs', cls: tone(k.expectancy) },
      { label: 'Max drawdown', value: pct(k.max_drawdown_pct), hint: 'from the running peak', cls: k.max_drawdown_pct > 10 ? 'neg' : 'flat' },
      { label: 'Costs', value: rupees(k.costs), hint: k.costs_pct_of_gross === null ? 'of gross profit' : `${pct(k.costs_pct_of_gross, 0)} of gross profit`, cls: 'flat' },
      { label: 'Best / worst trade', value: `${rupees(k.best, { sign: true })} / ${rupees(k.worst)}`, hint: `avg hold ${num(k.avg_hold_min, 0)} min`, cls: 'flat' },
      { label: 'Trades', value: String(k.trades), hint: `${k.wins} wins · ${k.losses} losses`, cls: 'flat' },
    ];
  });

  constructor() {
    effect(() => { const v = this.view(); if (location.hash.slice(1) !== v) history.replaceState(null, '', v === 'overview' ? location.pathname : `#${v}`); });
    addEventListener('hashchange', () => this.view.set(App.fromHash()));
  }
  private static fromHash(): View {
    const h = location.hash.slice(1);
    return (['overview', 'symbols', 'trades', 'system'] as const).find(v => v === h) ?? 'overview';
  }
  /** Minutes since an ISO timestamp with offset (e.g. 2026-09-25T11:19:00+05:30), for how long an open trade has been held. */
  heldMin(iso: string): number { const t = Date.parse(iso); return Number.isNaN(t) ? 0 : Math.max(0, Math.round((this.api.updatedAt()?.getTime() ?? Date.now()) / 60000 - t / 60000)); }
  plural(n: number, one: string, many = one + 's'): string { return `${n} ${n === 1 ? one : many}`; }

  setToken(el: HTMLInputElement): void { this.api.setToken(el.value); }
  ago(d: Date | null): string {
    return d ? d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Kolkata' }) + ' IST' : '–';
  }
  hours(v: number | null): string { return v === null ? '–' : v <= 0 ? 'expired' : `${v.toFixed(1)} h`; }
}
