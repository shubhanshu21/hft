import { longDate, shortDate, signed, tone, price, whyText } from "../../lib/format.js";
import { heldText, qtyHeader, qtyText, unitNoun } from "../../lib/replay.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

/** The trades of the chosen symbol as a table: when bought, when sold, prices, profit after fees. */
export default function SymbolTrades({ trades, days, onSelect, result }) {
  const paged = usePaged(trades, 10);
  const oneQty = !!result?.lots;           // futures results: a position is always sold whole, so one quantity column
  if (!trades.length) return <div className="none">The strategy has not traded this {unitNoun(result)}{trades.length === 0 ? " yet" : ""}.</div>;
  return (
    <div className="table-wrap">
      <table className="trades-table clickable">
        <thead><tr><th>Bought / shorted</th><th>{qtyHeader(result)}</th><th>Price</th><th>Sold / covered</th>{!oneQty && <th>{qtyHeader(result)}</th>}<th>Price</th><th>Held</th><th>Profit</th><th>Why it ended</th></tr></thead>
        <tbody>
          {paged.rows.map((t, k) => (
            <tr key={k} title="Zoom the chart to this trade" onClick={() => onSelect?.(t)}>
              <td>{t.side === "short" ? <b>SHORT </b> : null}{longDate(days[t.i0])}{t.adds?.length ? ` + added ${shortDate(days[t.adds[0].i])}` : ""}</td>
              <td>{qtyText(result, t.sym, t.qty)}{t.adds?.length ? ` + ${t.adds[0].q ?? "?"}` : ""}</td><td>{price(t.entry)}{t.adds?.length ? ` / ${price(t.adds[0].px)}` : ""}</td>
              <td>{t.i1 != null ? longDate(days[t.i1]) : "open"}</td>{!oneQty && <td>{qtyText(result, t.sym, t.soldQty)}</td>}<td>{price(t.exit)}</td>
              <td>{heldText(t)}</td>
              <td className={t.pnl != null ? tone(t.pnl) : ""}>{t.pnl != null ? signed(t.pnl) : "-"}</td>
              <td>{t.why ? whyText(t.why) : "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager paged={paged} />
    </div>
  );
}
