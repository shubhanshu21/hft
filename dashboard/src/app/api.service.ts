import { HttpClient, HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { Injectable, OnDestroy, inject, signal } from '@angular/core';
import { forkJoin } from 'rxjs';
import { BlockedSummary, DailyRow, EquityPoint, ExitRow, Overview, SymbolRow, SystemInfo, TradeRow } from './models';

const TOKEN_KEY = 'hft-dashboard-token';
const REFRESH_MS = 20_000;

@Injectable({ providedIn: 'root' })
export class ApiService implements OnDestroy {
  private http = inject(HttpClient);
  private timer: ReturnType<typeof setInterval> | undefined;

  readonly overview = signal<Overview | null>(null);
  readonly equity = signal<EquityPoint[]>([]);
  readonly daily = signal<DailyRow[]>([]);
  readonly symbols = signal<SymbolRow[]>([]);
  readonly exits = signal<ExitRow[]>([]);
  readonly trades = signal<TradeRow[]>([]);
  readonly system = signal<SystemInfo | null>(null);
  readonly blocked = signal<BlockedSummary | null>(null);

  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly needsToken = signal(false);
  readonly updatedAt = signal<Date | null>(null);

  constructor() {
    const fromUrl = new URLSearchParams(location.search).get('token');   // ?token=... once, then kept for the tab's session
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
      history.replaceState(null, '', location.pathname);                 // don't leave the token in the address bar / history
    }
    this.refresh();
    this.timer = setInterval(() => this.refresh(), REFRESH_MS);
  }

  ngOnDestroy(): void { clearInterval(this.timer); }

  setToken(token: string): void {
    sessionStorage.setItem(TOKEN_KEY, token.trim());
    this.needsToken.set(false);
    this.refresh();
  }

  private headers(): HttpHeaders {
    const t = sessionStorage.getItem(TOKEN_KEY);
    return t ? new HttpHeaders({ Authorization: `Bearer ${t}` }) : new HttpHeaders();
  }

  refresh(): void {
    const h = { headers: this.headers() };
    forkJoin({
      overview: this.http.get<Overview>('/api/overview', h),
      equity: this.http.get<EquityPoint[]>('/api/equity', h),
      daily: this.http.get<DailyRow[]>('/api/daily', h),
      symbols: this.http.get<SymbolRow[]>('/api/symbols', h),
      exits: this.http.get<ExitRow[]>('/api/exits', h),
      trades: this.http.get<TradeRow[]>('/api/trades?limit=200', h),
      system: this.http.get<SystemInfo>('/api/system', h),
      blocked: this.http.get<BlockedSummary>('/api/blocked', h),
    }).subscribe({
      next: r => {
        this.overview.set(r.overview); this.equity.set(r.equity); this.daily.set(r.daily); this.symbols.set(r.symbols);
        this.exits.set(r.exits); this.trades.set(r.trades); this.system.set(r.system); this.blocked.set(r.blocked);
        this.error.set(null); this.needsToken.set(false); this.loading.set(false); this.updatedAt.set(new Date());
      },
      error: (e: HttpErrorResponse) => {
        this.loading.set(false);
        if (e.status === 401) { this.needsToken.set(true); this.error.set('This dashboard needs an access token.'); }
        else this.error.set(e.status === 0 ? 'Cannot reach the dashboard API.' : `The API returned an error (${e.status}).`);
      },
    });
  }
}
