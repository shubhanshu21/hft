import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

export interface Slice { label: string; value: number; color: string }

@Component({
  selector: 'app-donut',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="row">
      <svg viewBox="0 0 120 120" role="img" aria-label="Share of trades by exit reason">
        <circle cx="60" cy="60" r="44" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="16" />
        @for (a of arcs(); track a.label) {
          <circle cx="60" cy="60" r="44" fill="none" [attr.stroke]="a.color" stroke-width="16" [attr.stroke-dasharray]="a.dash" [attr.stroke-dashoffset]="a.offset"
                  transform="rotate(-90 60 60)" />
        }
        <text x="60" y="58" text-anchor="middle" class="big">{{ total() }}</text>
        <text x="60" y="73" text-anchor="middle" class="small">trades</text>
      </svg>
      <ul>
        @for (s of slices(); track s.label) {
          <li><i [style.background]="s.color"></i><span>{{ s.label }}</span><b>{{ s.value }}</b></li>
        }
      </ul>
    </div>
  `,
  styles: [`
    .row { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
    svg { width: 130px; height: 130px; flex: none; } .big { fill: #e6e8f0; font-size: 22px; font-weight: 700; } .small { fill: #8a92a6; font-size: 9px; }
    ul { list-style: none; margin: 0; padding: 0; flex: 1; min-width: 150px; display: grid; gap: 7px; }
    li { display: flex; align-items: center; gap: 8px; font-size: 13px; } li i { width: 9px; height: 9px; border-radius: 50%; flex: none; }
    li span { flex: 1; color: #b5bccd; } li b { color: #e6e8f0; }
  `],
})
export class Donut {
  readonly slices = input.required<Slice[]>();
  readonly total = computed(() => this.slices().reduce((a, s) => a + s.value, 0));
  readonly arcs = computed(() => {
    const C = 2 * Math.PI * 44, total = this.total() || 1;
    let acc = 0;
    return this.slices().map(s => {
      const len = (s.value / total) * C;
      const arc = { label: s.label, color: s.color, dash: `${Math.max(0, len - 1.5).toFixed(2)} ${(C - Math.max(0, len - 1.5)).toFixed(2)}`, offset: (-acc).toFixed(2) };
      acc += len;
      return arc;
    });
  });
}
