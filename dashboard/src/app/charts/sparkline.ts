import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

@Component({
  selector: 'app-sparkline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">@if (d()) { <path [attr.d]="d()" fill="none" [attr.stroke]="color()" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round" /> }</svg>`,
  styles: [`svg { width: 100px; height: 28px; display: block; }`],
})
export class Sparkline {
  readonly values = input.required<number[]>();
  readonly color = computed(() => {
    const v = this.values();
    return v.length && v[v.length - 1] >= 0 ? '#29d391' : '#ff5d73';
  });
  readonly d = computed(() => {
    const v = this.values();
    if (v.length < 2) return '';
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    return v.map((y, i) => `${i ? 'L' : 'M'}${((i / (v.length - 1)) * 100).toFixed(1)},${(28 - ((y - lo) / span) * 26 - 1).toFixed(1)}`).join('');
  });
}
