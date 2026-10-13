import { monthName } from "../../lib/format.js";

const X0 = 54, X1 = 884, Y0 = 14, Y1 = 214;

export default function EquityChart({ result, upto, start }) {
  const { days, equity, events } = result;
  const N = days.length;
  const lo = Math.min(start, ...equity) - 1500, hi = Math.max(start, ...equity) + 1500;
  const sx = (i) => X0 + ((X1 - X0) * i) / Math.max(N - 1, 1);
  const sy = (v) => Y1 - ((Y1 - Y0) * (v - lo)) / (hi - lo);
  const step = Math.max(1000, Math.ceil((hi - lo) / 4 / 1000) * 1000);
  const grid = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) grid.push(v);
  const months = days.map((d, k) => [k, d]).filter(([k, d], _i, all) => k === 0 || d.slice(0, 7) !== days[k - 1].slice(0, 7)).filter((_, i, a) => a.length <= 14 || i % 2 === 0);
  const pts = equity.slice(0, upto + 1).map((v, k) => `${sx(k).toFixed(1)},${sy(v).toFixed(1)}`).join(" ");
  const col = equity[upto] >= start ? "var(--gain)" : "var(--loss)";
  const dots = [];
  for (let k = 0; k <= upto; k++) for (const [j, e] of events[k].entries()) {
    if (e.t === "sell") dots.push(<circle key={`${k}-${j}`} cx={sx(k)} cy={sy(equity[k])} r="3.5" fill={e.pnl >= 0 ? "var(--gain)" : "var(--loss)"} stroke="var(--panel)" strokeWidth="1.5" />);
  }
  return (
    <svg viewBox="0 0 900 250" role="img" aria-label="Account value over time">
      {grid.map((v) => (
        <g key={v}>
          <line x1={X0} x2={X1} y1={sy(v)} y2={sy(v)} stroke="var(--grid)" />
          <text x={X0 - 6} y={sy(v) + 4} textAnchor="end">{(v / 1000).toFixed(0)}k</text>
        </g>
      ))}
      <line x1={X0} x2={X1} y1={sy(start)} y2={sy(start)} stroke="var(--muted)" strokeDasharray="4 4" />
      {months.map(([k, d]) => <text key={d} x={sx(k)} y="236">{monthName(d)}</text>)}
      <polygon points={`${sx(0)},${sy(start)} ${pts} ${sx(upto)},${sy(start)}`} fill={col} opacity=".12" />
      <polyline points={pts} fill="none" stroke={col} strokeWidth="2" strokeLinejoin="round" />
      {dots}
      <circle cx={sx(upto)} cy={sy(equity[upto])} r="5" fill={col} stroke="var(--panel)" strokeWidth="2" />
    </svg>
  );
}
