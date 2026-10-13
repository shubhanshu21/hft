/** Small candlestick strip of a holding since it was bought, with a dashed line at the buy price. */
export default function MiniCandles({ bars, from, to, entry }) {
  const rows = [];
  for (let i = Math.max(from - 1, 0); i <= to; i++) if (bars[i]) rows.push(bars[i]);
  if (rows.length < 2) return null;
  const W = 96, H = 38, mn = Math.min(...rows.map((r) => r[2]), entry), mx = Math.max(...rows.map((r) => r[1]), entry);
  const y = (v) => H - 3 - ((H - 6) * (v - mn)) / (mx - mn || 1);
  const step = (W - 4) / rows.length, w = Math.max(1.5, Math.min(step * 0.7, 6));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true">
      <line x1="0" x2={W} y1={y(entry)} y2={y(entry)} stroke="var(--muted)" strokeDasharray="2 3" />
      {rows.map(([o, h, l, c], k) => {
        const cx = 2 + step * (k + 0.5), col = c >= o ? "var(--gain)" : "var(--loss)";
        return (
          <g key={k}>
            <line x1={cx} x2={cx} y1={y(h)} y2={y(l)} stroke={col} strokeWidth="1" />
            <rect x={cx - w / 2} y={y(Math.max(o, c))} width={w} height={Math.max(1, Math.abs(y(o) - y(c)))} fill={col} />
          </g>
        );
      })}
    </svg>
  );
}
