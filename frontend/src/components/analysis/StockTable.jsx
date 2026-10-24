import { useState } from "react";
import Panel from "../ui/Panel.jsx";
import { profitByStock } from "../../lib/analysis.js";
import { unitNoun } from "../../lib/replay.js";
import { inr, signed, tone } from "../../lib/format.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

const cols = (noun) => [["sym", noun[0].toUpperCase() + noun.slice(1)], ["trades", "Trades"], ["wins", "Winners"], ["profit", "Profit after fees"], ["fees", "Fees"]];

/** Profit per stock over the whole run, sortable. Clicking a stock shows it on the chart. */
export default function StockTable({ result, onPick }) {
  const [sort, setSort] = useState(["trades", -1]);
  const rows = profitByStock(result).sort((a, b) => (a[sort[0]] < b[sort[0]] ? -1 : a[sort[0]] > b[sort[0]] ? 1 : 0) * sort[1]);
  const paged = usePaged(rows, 10);
  return (
    <Panel title={`Profit by ${unitNoun(result)}`}>
      <div className="table-wrap">
        <table className="trades-table clickable">
          <thead><tr>{cols(unitNoun(result)).map(([k, l]) => <th key={k}><button type="button" className="th-btn" onClick={() => setSort([k, sort[0] === k ? -sort[1] : -1])}>{l}{sort[0] === k ? (sort[1] < 0 ? " ↓" : " ↑") : ""}</button></th>)}</tr></thead>
          <tbody>{paged.rows.map((r) => (
            <tr key={r.sym} onClick={() => onPick?.(r.sym)}>
              <td>{r.sym}</td><td>{r.trades}{r.open ? ` + ${r.open} open` : ""}</td><td>{r.trades ? `${r.wins} (${Math.round(r.wins / r.trades * 100)}%)` : "-"}</td>
              <td className={tone(r.profit)}>{signed(r.profit)}</td><td>{inr(r.fees)}</td>
            </tr>))}</tbody>
        </table>
      </div>
      <Pager paged={paged} />
    </Panel>
  );
}
