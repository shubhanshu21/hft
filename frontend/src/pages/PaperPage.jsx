import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import Panel from "../components/ui/Panel.jsx";
import Stat from "../components/ui/Stat.jsx";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getPaper } from "../api/client.js";
import { useTheme } from "../hooks/useTheme.js";
import { inr, signed, tone } from "../lib/format.js";

const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "-");

/** The paper-trading account as it is now (read-only, refreshed every minute). No real orders are ever placed. */
export default function PaperPage() {
  const colors = useTheme();
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  const box = useRef(null);

  useEffect(() => {
    const load = () => getPaper().then((x) => { setD(x); setError(""); }).catch((e) => setError(e.message));
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!box.current || !d?.equity?.length) return undefined;
    const chart = createChart(box.current, baseOptions(colors, 240));
    const s = chart.addLineSeries({ color: colors.accent, lineWidth: 2, priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` } });
    s.setData(d.equity.map((e) => ({ time: e.date, value: e.current_capital })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [d, colors]);

  if (error) return <div className="wrap"><div className="panel error" role="alert">{error}</div></div>;
  if (!d) return <div className="wrap"><div className="panel none">Loading the paper account...</div></div>;
  if (!d.available) return <div className="wrap"><div className="panel none">There is no paper-trading account yet.</div></div>;

  const acc = d.account ?? {};
  const unreal = d.positions.reduce((a, p) => a + (p.last_close != null ? (p.last_close - p.entry_price) * p.qty * (p.direction === "short" ? -1 : 1) : 0), 0);
  const value = (acc.current_capital ?? 0) + unreal;
  return (
    <div className="wrap">
      <div className="page-head">
        <div><h1>Paper trading</h1><div className="sub">The practice account the daemon trades with real prices and no real money. Refreshes every minute. Open positions are valued at the latest daily close on file.</div></div>
        <div className="big"><div className="eq">{inr(value)}</div><div className="chg"><span className={tone(value - acc.initial_capital)}>{signed(value - acc.initial_capital)}</span> since {inr(acc.initial_capital)}</div></div>
      </div>
      <Panel className="stats">
        <Stat label="Booked profit" value={<span className={tone(d.stats.net)}>{signed(d.stats.net)}</span>} />
        <Stat label="Not sold yet" value={<span className={tone(unreal)}>{signed(unreal)}</span>} />
        <Stat label="Closed trades" value={d.stats.closed} />
        <Stat label="Winners" value={d.stats.closed ? `${d.stats.wins} of ${d.stats.closed}` : "-"} />
        <Stat label="Fees paid" value={inr(d.stats.fees)} />
        <Stat label="Open now" value={d.positions.length} />
      </Panel>
      <Panel title="Holding now">
        {!d.positions.length ? <div className="none">No open positions. The strategy only buys between 9:15 and 10:00 when its rules are met.</div> : (
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>Stock</th><th>Strategy</th><th>Bought</th><th>Qty</th><th>Buy price</th><th>Stop now</th><th>Last close</th><th>Not sold yet</th></tr></thead>
            <tbody>{d.positions.map((p) => {
              const u = p.last_close != null ? (p.last_close - p.entry_price) * p.qty * (p.direction === "short" ? -1 : 1) : null;
              return <tr key={p.symbol}><td>{p.symbol}</td><td>{p.strategy}</td><td>{when(p.entry_time)}</td><td>{p.qty}</td><td>{p.entry_price.toFixed(2)}</td>
                <td>{p.current_stop?.toFixed(2)}</td><td>{p.last_close?.toFixed(2) ?? "-"} <span className="note">{p.last_date}</span></td><td className={u != null ? tone(u) : ""}>{u != null ? signed(u) : "-"}</td></tr>;
            })}</tbody>
          </table></div>)}
      </Panel>
      <Panel title="Account value by day">{d.equity.length ? <div ref={box} className="tv-box" /> : <div className="none">No daily snapshots yet.</div>}</Panel>
      <Panel title="Closed trades">
        {!d.trades.length ? <div className="none">No closed trades yet.</div> : (
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>Stock</th><th>Bought</th><th>Sold</th><th>Qty</th><th>Buy</th><th>Sell</th><th>Fees</th><th>Profit after fees</th><th>Why it ended</th></tr></thead>
            <tbody>{d.trades.map((t, i) => <tr key={i}><td>{t.symbol}</td><td>{when(t.entry_dt)}</td><td>{when(t.exit_dt)}</td><td>{t.qty}</td><td>{t.entry_price?.toFixed(2)}</td>
              <td>{t.exit_price?.toFixed(2)}</td><td>{inr(t.total_friction ?? 0)}</td><td className={tone(t.net_pnl ?? 0)}>{signed(t.net_pnl ?? 0)}</td><td>{(t.exit_reason ?? "").replace(/_/g, " ")}</td></tr>)}</tbody>
          </table></div>)}
      </Panel>
    </div>
  );
}
