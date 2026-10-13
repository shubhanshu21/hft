import { shortDate } from "../../lib/format.js";

const W = 900, H = 300, X0 = 56, X1 = 888, Y0 = 12, Y1 = 262;

/**
 * Candlestick chart of one symbol from day `first` to day `at`, with every trade the strategy took in it:
 * a triangle at each buy and sell, and a line from entry to exit (green = profit, red = loss, dashed while still open).
 * bars[i] = [open, high, low, close] or null (holiday / before the first candle). trades = [{ i0, i1, entry, exit, pnl }].
 */
export default function CandleChart({ symbol, bars, days, first, last, at, trades }) {
  const idx = [];
  for (let i = first; i <= at; i++) if (bars[i]) idx.push(i);
  if (!idx.length) return <div className="none">No candles for {symbol} in this range yet.</div>;

  const shown = trades.filter((t) => (t.i1 ?? at) >= first);
  const span = [];                                    // price scale fixed over the whole x-axis so it does not jump while playing
  for (let i = first; i <= last; i++) if (bars[i]) span.push(i);
  let lo = Math.min(...span.map((i) => bars[i][2]), ...shown.flatMap((t) => [t.entry, t.exit ?? Infinity]));
  let hi = Math.max(...span.map((i) => bars[i][1]), ...shown.flatMap((t) => [t.entry, t.exit ?? -Infinity]));
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad; hi += pad;

  const slots = last - first + 1;
  const step = (X1 - X0) / Math.max(slots, 1);
  const x = (i) => X0 + step * (i - first + 0.5);
  const y = (v) => Y1 - ((Y1 - Y0) * (v - lo)) / (hi - lo);
  const body = Math.max(1.2, Math.min(step * 0.7, 14));
  const size = slots > 400 ? 4 : 6;

  const rough = (hi - lo) / 5, mag = 10 ** Math.floor(Math.log10(rough));
  const tickStep = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= rough);
  const ticks = [];
  for (let v = Math.ceil(lo / tickStep) * tickStep; v <= hi; v += tickStep) ticks.push(v);
  const labelEvery = Math.max(1, Math.ceil(slots / 8));
  const axis = [];
  for (let i = first; i <= last; i += labelEvery) axis.push(i);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${symbol} candlesticks with the strategy's trades`}>
      {ticks.map((v) => (
        <g key={v}>
          <line x1={X0} x2={X1} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
          <text x={X0 - 6} y={y(v) + 4} textAnchor="end">{v >= 1000 ? v.toFixed(0) : v.toFixed(2)}</text>
        </g>
      ))}
      {idx.map((i) => {
        const [o, h, l, c] = bars[i];
        const up = c >= o, col = up ? "var(--gain)" : "var(--loss)";
        return (
          <g key={i}>
            <line x1={x(i)} x2={x(i)} y1={y(h)} y2={y(l)} stroke={col} strokeWidth="1" />
            <rect x={x(i) - body / 2} y={y(Math.max(o, c))} width={body} height={Math.max(1, Math.abs(y(o) - y(c)))} fill={up ? "var(--panel)" : col} stroke={col} strokeWidth="1" />
          </g>
        );
      })}
      {shown.map((t, k) => {
        const done = t.i1 != null, win = done ? t.pnl >= 0 : true;
        const col = done ? (win ? "var(--gain)" : "var(--loss)") : "var(--accent)";
        const x0 = x(Math.max(t.i0, first)), x1 = done ? x(Math.min(t.i1, at)) : x(at);
        return (
          <g key={k}>
            <line x1={x0} y1={y(t.entry)} x2={x1} y2={done ? y(t.exit) : y(t.entry)} stroke={col} strokeWidth="2" strokeDasharray={done ? undefined : "5 4"} opacity=".9" />
            {t.i0 >= first && <polygon points={`${x(t.i0)},${y(t.entry) - 1} ${x(t.i0) - size},${y(t.entry) + size * 1.6} ${x(t.i0) + size},${y(t.entry) + size * 1.6}`} fill={col} stroke="var(--panel)" strokeWidth="1" />}
            {done && <polygon points={`${x(t.i1)},${y(t.exit) + 1} ${x(t.i1) - size},${y(t.exit) - size * 1.6} ${x(t.i1) + size},${y(t.exit) - size * 1.6}`} fill={col} stroke="var(--panel)" strokeWidth="1" />}
          </g>
        );
      })}
      {axis.map((i) => <text key={i} x={x(i)} y={H - 18} textAnchor="middle">{shortDate(days[i])}</text>)}
    </svg>
  );
}
