import { shortDate, signed, tone } from "../../lib/format.js";
import { qtyHeader, qtyText } from "../../lib/replay.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

/** The trades of the chosen symbol as a table: when bought, when sold, prices, profit after fees. */
export default function SymbolTrades({ trades, days, onSelect, result }) {
  const paged = usePaged(trades, 10);
  if (!trades.length) return <div className="none">The strategy has not traded this stock{trades.length === 0 ? " yet" : ""}.</div>;
  return (
    <div className="table-wrap">
      <table className="trades-table clickable">
        <thead><tr><th>Bought</th><th>{qtyHeader(result)}</th><th>Price</th><th>Sold</th><th>{qtyHeader(result)}</th><th>Price</th><th>Held</th><th>Profit</th><th>Why it ended</th></tr></thead>
        <tbody>
          {paged.rows.map((t, k) => (
            <tr key={k} title="Zoom the chart to this trade" onClick={() => onSelect?.(t)}>
              <td>{shortDate(days[t.i0])}{t.adds?.length ? ` + added ${shortDate(days[t.adds[0].i])}` : ""}</td>
              <td>{qtyText(result, t.sym, t.qty)}{t.adds?.length ? ` + ${t.adds[0].q ?? "?"}` : ""}</td><td>{t.entry.toFixed(2)}{t.adds?.length ? ` / ${t.adds[0].px.toFixed(2)}` : ""}</td>
              <td>{t.i1 != null ? shortDate(days[t.i1]) : "open"}</td><td>{qtyText(result, t.sym, t.soldQty)}</td><td>{t.exit != null ? t.exit.toFixed(2) : "-"}</td>
              <td>{t.days != null ? `${t.days}d` : "-"}</td>
              <td className={t.pnl != null ? tone(t.pnl) : ""}>{t.pnl != null ? signed(t.pnl) : "-"}</td>
              <td>{t.why ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager paged={paged} />
    </div>
  );
}
