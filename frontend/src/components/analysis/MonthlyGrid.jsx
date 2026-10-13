import Panel from "../ui/Panel.jsx";
import { monthlyReturns } from "../../lib/analysis.js";

const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Account change per month, one row per year; green for gains, red for losses, stronger colour for bigger moves. */
function Cell({ v }) {
  if (v == null) return <td className="mg-empty" />;
  const a = Math.min(Math.abs(v) / 6, 1) * 0.55 + 0.08;
  return <td style={{ background: `color-mix(in srgb, ${v >= 0 ? "var(--gain)" : "var(--loss)"} ${Math.round(a * 100)}%, transparent)` }}>{v >= 0 ? "+" : ""}{v.toFixed(1)}</td>;
}

export default function MonthlyGrid({ result, start }) {
  const data = monthlyReturns(result.days, result.equity, start);
  const years = Object.keys(data).sort();
  return (
    <Panel title="Monthly returns (%)">
      <div className="table-wrap">
        <table className="monthly">
          <thead><tr><th>Year</th>{M.map((m) => <th key={m}>{m}</th>)}<th>Year</th></tr></thead>
          <tbody>{years.map((y) => <tr key={y}><th>{y}</th>{M.map((_, i) => <Cell key={i} v={data[y][i + 1]} />)}<Cell v={data[y].year} /></tr>)}</tbody>
        </table>
      </div>
    </Panel>
  );
}
