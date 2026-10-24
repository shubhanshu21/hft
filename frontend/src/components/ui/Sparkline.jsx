import { useId } from "react";

/** A tiny trend line with a soft area wash (2px line, ~15% fill), no axes: the shape is the message, the number next to it the value.
 *  `values` is downsampled to at most 160 points; `bench` (optional) draws a dashed comparison line on the same scale. */
export default function Sparkline({ values, bench = null, height = 48, color = null, fill = true, baseline = null, className = "" }) {
  const id = useId().replace(/:/g, "");
  const pick = (arr) => {
    const v = (arr ?? []).filter((x) => x != null && Number.isFinite(+x)).map(Number);
    if (v.length <= 160) return v;
    const step = (v.length - 1) / 159;
    return Array.from({ length: 160 }, (_, i) => v[Math.round(i * step)]);
  };
  const a = pick(values), b = bench ? pick(bench) : [];
  if (a.length < 2) return null;
  const all = [...a, ...b, ...(baseline != null ? [baseline] : [])];
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1, W = 300, H = 100;
  const pt = (arr) => arr.map((v, i) => `${((i / (arr.length - 1)) * W).toFixed(1)},${(H - 4 - ((v - lo) / span) * (H - 8)).toFixed(1)}`);
  const line = pt(a);
  const yBase = baseline != null ? H - 4 - ((baseline - lo) / span) * (H - 8) : H;
  return (
    <svg className={`spark ${className}`} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={color ? { height, color } : { height }} aria-hidden="true">
      <defs><linearGradient id={`g${id}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="currentColor" stopOpacity=".28" /><stop offset="1" stopColor="currentColor" stopOpacity="0" /></linearGradient></defs>
      {baseline != null && <line x1="0" x2={W} y1={yBase} y2={yBase} stroke="currentColor" strokeOpacity=".35" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />}
      {fill && <polygon points={`0,${H} ${line.join(" ")} ${W},${H}`} fill={`url(#g${id})`} />}
      {b.length > 1 && <polyline points={pt(b).join(" ")} fill="none" stroke="currentColor" strokeOpacity=".5" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />}
      <polyline points={line.join(" ")} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
