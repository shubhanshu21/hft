import { useEffect, useState } from "react";
import Panel from "../ui/Panel.jsx";
import { getEquityStatus } from "../../api/client.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

const short = (d) => new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/** What the equity trend strategy is waiting for: market health against its 50% rule, and each of its stocks' distance from a buy signal. */
export default function StrategyStatus() {
  const [s, setS] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => { getEquityStatus().then(setS).catch((e) => setError(e.message)); }, []);
  const paged = usePaged(s?.stocks ?? [], 10);
  if (error) return <Panel title="trend_ema_cross: what it is waiting for"><div className="none">{error}</div></Panel>;
  if (!s) return null;
  const healthy = s.breadth >= s.need;
  const ready = s.stocks.filter((x) => x.order === 0);
  return (
    <Panel title="trend_ema_cross: what it is waiting for">
      <p className={healthy ? "gain" : "warn inline"}>
        Market health on {short(s.as_of)}: <b>{s.breadth}%</b> of NIFTY 50 stocks are above their 50-day average. It buys only at <b>{s.need}%</b> or more,
        so it is {healthy ? "allowed to buy" : "waiting in cash"}.
      </p>
      <div className="sub">Last weeks: {s.breadth_history.slice(-12).map((b) => `${short(b.date)} ${Math.round(b.value)}%`).join(" · ")}</div>
      <p>{ready.length ? `Fresh signal today: ${ready.map((x) => x.symbol).join(", ")}.` : "No stock gave a fresh buy signal on the latest day."}</p>
      <div className="table-wrap"><table className="trades-table">
        <thead><tr><th>Stock</th><th>Last close</th><th>Above 200-day average</th><th>20-day vs 50-day average</th><th>Last crossover</th><th>Status</th></tr></thead>
        <tbody>{paged.rows.map((x) => (
          <tr key={x.symbol}>
            <td>{x.symbol}</td><td>{x.close.toFixed(2)}</td><td>{x.above_200 ? "yes" : "no"}</td>
            <td className={x.gap_pct >= 0 ? "gain" : "loss"}>{x.gap_pct >= 0 ? "+" : ""}{x.gap_pct.toFixed(1)}%</td>
            <td>{x.last_cross ? short(x.last_cross) : "-"}</td><td>{x.state}</td>
          </tr>
        ))}</tbody>
      </table></div>
      <Pager paged={paged} />
    </Panel>
  );
}
