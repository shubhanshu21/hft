import Panel from "../ui/Panel.jsx";
import { inr, pct, tone } from "../../lib/format.js";

/** One row per calendar year of the run: money at the start and end, the change, and that year's own worst fall from a high
 *  (2026-10-24 -- the good and bad years at a glance, instead of checking them by hand). */
export default function YearTable({ result, start }) {
  const rows = [];
  let cur = null;
  result.days.forEach((d, i) => {
    const y = d.slice(0, 4), v = result.equity[i];
    if (!cur || cur.y !== y) { cur = { y, from: i === 0 ? start : result.equity[i - 1], to: v, peak: Math.max(v, i === 0 ? start : result.equity[i - 1]), dd: 0 }; rows.push(cur); }
    cur.to = v; cur.peak = Math.max(cur.peak, v); cur.dd = Math.min(cur.dd, (v / cur.peak - 1) * 100);
  });
  return (
    <Panel title="Year by year">
      <div className="table-wrap">
        <table className="trades-table">
          <thead><tr><th>Year</th><th>Start</th><th>End</th><th>Change</th><th>Worst fall in the year</th></tr></thead>
          <tbody>{rows.map((r) => {
            const ch = (r.to / r.from - 1) * 100;
            return <tr key={r.y}><td><b>{r.y}</b></td><td>{inr(r.from)}</td><td>{inr(r.to)}</td><td className={tone(ch)}>{pct(ch, 1)}</td><td className={r.dd < 0 ? "down" : ""}>{r.dd.toFixed(1)}%</td></tr>;
          })}</tbody>
        </table>
      </div>
    </Panel>
  );
}
