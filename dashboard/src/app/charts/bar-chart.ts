import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

export interface Bar { label: string; value: number; sub?: string }

/** Signed bars around a zero line (daily P&L). Green up, red down. */
@Component({
  selector: 'app-bar-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="wrap">
      <svg [attr.viewBox]="'0 0 ' + W + ' ' + H" role="img" aria-label="Daily profit and loss">
        <line [attr.x1]="padL" [attr.x2]="W - padR" [attr.y1]="zeroY()" [attr.y2]="zeroY()" class="zero" />
        @for (b of geo(); track b.i) {
          <rect [attr.x]="b.x" [attr.y]="b.y" [attr.width]="b.w" [attr.height]="b.h" rx="3" [attr.fill]="b.value >= 0 ? '#29d391' : '#ff5d73'"
                [attr.opacity]="hover() === null || hover() === b.i ? 1 : 0.45" (pointerenter)="hover.set(b.i)" (pointerleave)="hover.set(null)" />
          @if (bars().length <= 14) { <text [attr.x]="b.x + b.w / 2" [attr.y]="H - 6" class="axis" text-anchor="middle">{{ b.label }}</text> }
        }
        <text [attr.x]="padL - 8" [attr.y]="padT + 8" class="axis" text-anchor="end">{{ format()(max()) }}</text>
        <text [attr.x]="padL - 8" [attr.y]="H - padB" class="axis" text-anchor="end">{{ format()(min()) }}</text>
      </svg>
      @if (hover() !== null) {
        <div class="tip"><b [class.pos]="bars()[hover()!].value > 0" [class.neg]="bars()[hover()!].value < 0">{{ format()(bars()[hover()!].value) }}</b>
          <span>{{ bars()[hover()!].label }}{{ bars()[hover()!].sub ? ' · ' + bars()[hover()!].sub : '' }}</span></div>
      }
      @if (!bars().length) { <div class="empty">No closed trades yet</div> }
    </div>
  `,
  styles: [`
    .wrap { position: relative; } svg { width: 100%; height: auto; display: block; }
    .zero { stroke: rgba(255,255,255,.22); } .axis { fill: #7f8aa3; font-size: 11px; }
    .tip { position: absolute; top: 0; right: 0; background: #1b2140; border: 1px solid rgba(255,255,255,.12); border-radius: 8px; padding: 6px 10px;
           display: flex; flex-direction: column; align-items: flex-end; font-size: 12px; pointer-events: none; }
    .tip span { color: #8a92a6; font-size: 11px; } .pos { color: #29d391; } .neg { color: #ff5d73; }
    .empty { position: absolute; inset: 0; display: grid; place-items: center; color: #7f8aa3; font-size: 13px; }
  `],
})
export class BarChart {
  readonly bars = input.required<Bar[]>();
  readonly format = input<(v: number) => string>(v => v.toFixed(0));
  readonly W = 640; readonly H = 200; readonly padL = 64; readonly padR = 10; readonly padT = 10; readonly padB = 24;
  readonly hover = signal<number | null>(null);

  readonly max = computed(() => Math.max(0, ...this.bars().map(b => b.value)));
  readonly min = computed(() => Math.min(0, ...this.bars().map(b => b.value)));
  private readonly span = computed(() => (this.max() - this.min()) || 1);
  private y(v: number): number { return this.padT + (1 - (v - this.min()) / this.span()) * (this.H - this.padT - this.padB); }
  readonly zeroY = computed(() => this.y(0));
  readonly geo = computed(() => {
    const n = this.bars().length || 1;
    const slot = (this.W - this.padL - this.padR) / n;
    const w = Math.min(46, slot * 0.66);
    return this.bars().map((b, i) => {
      const top = this.y(Math.max(b.value, 0)), bottom = this.y(Math.min(b.value, 0));
      return { i, value: b.value, label: b.label, x: this.padL + i * slot + (slot - w) / 2, y: top, w, h: Math.max(2, bottom - top) };
    });
  });
}
