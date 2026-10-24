/** "Where the money went" as a bridge (waterfall) chart: each step floats from where the last one ended; grey = a balance, green = money
 *  in, red = money out, blue = what you keep. Values sit above each column in text ink (the sign carries direction, not just colour);
 *  thin dashed connectors join the steps. `steps` come from MetricsGrid's moneySteps(). */
export default function Bridge({ steps }) {
  const n = steps.length, W = 640, H = 270, top = 30, bottom = 46, plotH = H - top - bottom;
  const max = Math.max(...steps.map((s) => s.hi), 1) * 1.04;
  const y = (v) => top + plotH - (v / max) * plotH;
  const band = W / n, bw = Math.min(54, band * 0.46);
  const cx = (i) => band * i + band / 2;
  return (
    <svg className="bridge" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Where the money went, step by step">
      <line x1="0" x2={W} y1={y(0)} y2={y(0)} className="bridge-base" />
      {steps.map((s, i) => {
        const x = cx(i) - bw / 2, y0 = y(s.hi), h = Math.max(2, y(s.lo) - y(s.hi));
        const next = steps[i + 1];
        const joinV = s.kind === "loss" ? s.lo : s.hi;
        return (
          <g key={s.label}>
            <title>{`${s.label}: ${s.amount}`}</title>
            {next && <line x1={cx(i) + bw / 2} x2={cx(i + 1) - bw / 2} y1={y(joinV)} y2={y(joinV)} className="bridge-join" />}
            <rect x={x} y={y0} width={bw} height={h} rx="4" className={`bridge-bar ${s.kind}`} />
            <text x={cx(i)} y={y0 - 9} textAnchor="middle" className={`bridge-val${s.final ? " final" : ""}`}>{s.amount}</text>
            <text x={cx(i)} y={H - bottom + 20} textAnchor="middle" className="bridge-lab">{s.short}</text>
          </g>
        );
      })}
    </svg>
  );
}
