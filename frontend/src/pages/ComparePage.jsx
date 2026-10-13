import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import Panel from "../components/ui/Panel.jsx";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getRun, listRuns } from "../api/client.js";
import { useTheme } from "../hooks/useTheme.js";
import { pct } from "../lib/format.js";

const MAX = 4;
const name = (r) => r.label || `${r.strategy} on ${r.market}, ${r.params?.start} to ${r.params?.end}`;

/** Two to four saved runs side by side: their % return over time on one chart, and their key numbers in one table. */
export default function ComparePage() {
  const colors = useTheme();
  const palette = [colors.accent, colors.gain, colors.loss, colors.muted];
  const [runs, setRuns] = useState([]);
  const [picked, setPicked] = useState([]);
  const [data, setData] = useState({});
  const box = useRef(null);

  useEffect(() => { listRuns().then((r) => setRuns(r.filter((x) => x.status === "done"))).catch(() => {}); }, []);
  useEffect(() => { for (const id of picked) if (!data[id]) getRun(id).then((r) => setData((d) => ({ ...d, [id]: r }))).catch(() => {}); }, [picked]);

  useEffect(() => {
    if (!box.current) return undefined;
    const chart = createChart(box.current, baseOptions(colors, 360));
    picked.forEach((id, k) => {
      const r = data[id]?.result;
      if (!r) return;
      const start = r.metrics?.start_capital ?? r.equity[0];
      const s = chart.addLineSeries({ color: palette[k], lineWidth: 2, title: `${k + 1}`, priceFormat: { type: "custom", formatter: (v) => `${v.toFixed(1)}%` } });
      s.setData(r.days.map((d, i) => ({ time: d, value: (r.equity[i] / start - 1) * 100 })));
    });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [picked, data, colors]);

  const toggle = (id) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length < MAX ? [...p, id] : p));
  const rows = [["Return", (m) => pct(m.return_pct, 2)], ["Per year (CAGR)", (m) => pct(m.cagr_pct, 1)], ["Worst fall", (m) => `${m.max_drawdown_pct}%`],
                ["Trades", (m) => m.trades], ["Win rate", (m) => `${m.win_rate_pct}%`], ["Profit factor", (m) => m.profit_factor ?? "-"],
                ["Fees", (m) => `₹${Number(m.fees ?? 0).toLocaleString("en-IN")}`]];
  const prow = [["Risk per trade", (p) => `${p.risk_pct}%`], ["Most holdings", (p) => p.max_positions], ["Leverage", (p) => `${p.leverage}x`], ["Slippage", (p) => `${p.slippage_bp} bp`]];

  return (
    <div className="wrap">
      <div className="page-head"><div><h1>Compare runs</h1><div className="sub">Tick up to {MAX} saved runs. The chart shows each one's % gain or loss over time, so runs with different starting money can be compared.</div></div></div>
      <Panel title="Saved runs">
        {!runs.length && <div className="none">No saved runs yet. Run a backtest first.</div>}
        <div className="pick-list">
          {runs.map((r) => (
            <label key={r.id} className="check">
              <input type="checkbox" checked={picked.includes(r.id)} onChange={() => toggle(r.id)} disabled={!picked.includes(r.id) && picked.length >= MAX} />
              {picked.includes(r.id) && <span className="swatch" style={{ background: palette[picked.indexOf(r.id)] }} />}
              {name(r)} {r.metrics ? `(${r.metrics.return_pct >= 0 ? "+" : ""}${r.metrics.return_pct}%)` : ""}
            </label>
          ))}
        </div>
      </Panel>
      {picked.length > 0 && (
        <>
          <Panel title="Gain or loss over time (%)"><div ref={box} className="tv-box" /></Panel>
          <Panel title="Side by side">
            <div className="table-wrap">
              <table className="trades-table">
                <thead><tr><th />{picked.map((id, k) => <th key={id}><span className="swatch" style={{ background: palette[k] }} /> {data[id] ? name(data[id]) : "Loading..."}</th>)}</tr></thead>
                <tbody>
                  {rows.map(([l, f]) => <tr key={l}><th>{l}</th>{picked.map((id) => <td key={id}>{data[id]?.result?.metrics ? f(data[id].result.metrics) : ""}</td>)}</tr>)}
                  {prow.map(([l, f]) => <tr key={l}><th>{l}</th>{picked.map((id) => <td key={id}>{data[id]?.params ? f(data[id].params) : ""}</td>)}</tr>)}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
