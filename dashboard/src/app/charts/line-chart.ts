import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

export interface LinePoint { label: string; y: number }

/** Area/line chart in plain SVG with y-axis grid, a zero/baseline option and a hover read-out. */
@Component({
  selector: 'app-line-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="wrap">
      <svg [attr.viewBox]="'0 0 ' + W + ' ' + H" (pointermove)="move($event)" (pointerleave)="hover.set(null)" role="img" [attr.aria-label]="ariaLabel()">
        <defs>
          <linearGradient [attr.id]="gid" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" [attr.stop-color]="color()" stop-opacity="0.35" />
            <stop offset="100%" [attr.stop-color]="color()" stop-opacity="0" />
          </linearGradient>
        </defs>
        @for (t of ticks(); track t.y) {
          <line [attr.x1]="padL" [attr.x2]="W - padR" [attr.y1]="t.y" [attr.y2]="t.y" class="grid" />
          <text [attr.x]="padL - 8" [attr.y]="t.y + 4" class="axis" text-anchor="end">{{ t.text }}</text>
        }
        @if (area(); as a) {
          <path [attr.d]="a" [attr.fill]="'url(#' + gid + ')'" />
          <path [attr.d]="line()" fill="none" [attr.stroke]="color()" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
        }
        @if (xLabels().length) {
          <text [attr.x]="padL" [attr.y]="H - 6" class="axis">{{ xLabels()[0] }}</text>
          <text [attr.x]="W - padR" [attr.y]="H - 6" class="axis" text-anchor="end">{{ xLabels()[1] }}</text>
        }
        @if (hovered(); as h) {
          <line [attr.x1]="h.x" [attr.x2]="h.x" [attr.y1]="padT" [attr.y2]="H - padB" class="cursor" />
          <circle [attr.cx]="h.x" [attr.cy]="h.py" r="4" [attr.fill]="color()" stroke="#0b1020" stroke-width="2" />
        }
      </svg>
      @if (hovered(); as h) {
        <div class="tip" [style.left.%]="(h.x / W) * 100">
          <b>{{ format()(h.y) }}</b><span>{{ h.label }}</span>
        </div>
      }
      @if (points().length < 2) { <div class="empty">Not enough data yet</div> }
    </div>
  `,
  styles: [`
    .wrap { position: relative; }
    svg { width: 100%; height: auto; display: block; }
    .grid { stroke: rgba(255,255,255,.07); stroke-width: 1; }
    .axis { fill: #7f8aa3; font-size: 11px; }
    .cursor { stroke: rgba(255,255,255,.25); stroke-dasharray: 3 3; }
    .tip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; background: #1b2140; border: 1px solid rgba(255,255,255,.12);
           border-radius: 8px; padding: 6px 10px; display: flex; flex-direction: column; align-items: center; font-size: 12px; white-space: nowrap; }
    .tip span { color: #8a92a6; font-size: 11px; }
    .empty { position: absolute; inset: 0; display: grid; place-items: center; color: #7f8aa3; font-size: 13px; }
  `],
})
export class LineChart {
  readonly points = input.required<LinePoint[]>();
  readonly color = input('#a05cff');
  readonly format = input<(v: number) => string>(v => v.toFixed(0));
  readonly ariaLabel = input('Line chart');

  readonly W = 640; readonly H = 230; readonly padL = 64; readonly padR = 14; readonly padT = 12; readonly padB = 24;
  readonly gid = 'g' + Math.random().toString(36).slice(2, 8);
  readonly hover = signal<number | null>(null);

  private readonly scale = computed(() => {
    const ys = this.points().map(p => p.y);
    if (ys.length < 2) return null;
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const n = ys.length;
    const x = (i: number) => this.padL + (i / (n - 1)) * (this.W - this.padL - this.padR);
    const y = (v: number) => this.padT + (1 - (v - lo) / (hi - lo)) * (this.H - this.padT - this.padB);
    return { lo, hi, x, y };
  });
  readonly line = computed(() => {
    const s = this.scale();
    return s ? this.points().map((p, i) => `${i ? 'L' : 'M'}${s.x(i).toFixed(1)},${s.y(p.y).toFixed(1)}`).join('') : '';
  });
  readonly area = computed(() => {
    const s = this.scale();
    if (!s) return '';
    const n = this.points().length;
    return `${this.line()}L${s.x(n - 1).toFixed(1)},${this.H - this.padB}L${s.x(0).toFixed(1)},${this.H - this.padB}Z`;
  });
  readonly ticks = computed(() => {
    const s = this.scale();
    if (!s) return [];
    return [0, 1, 2, 3].map(i => {
      const v = s.lo + ((s.hi - s.lo) * i) / 3;
      return { y: s.y(v), text: this.format()(v) };
    });
  });
  readonly xLabels = computed(() => {
    const p = this.points();
    return p.length > 1 ? [p[0].label, p[p.length - 1].label] : [];
  });
  readonly hovered = computed(() => {
    const s = this.scale(), i = this.hover();
    if (!s || i === null) return null;
    const p = this.points()[i];
    return { x: s.x(i), py: s.y(p.y), y: p.y, label: p.label };
  });

  move(e: PointerEvent): void {
    const n = this.points().length;
    if (n < 2) return;
    const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * this.W;
    const f = (px - this.padL) / (this.W - this.padL - this.padR);
    this.hover.set(Math.max(0, Math.min(n - 1, Math.round(f * (n - 1)))));
  }
}
