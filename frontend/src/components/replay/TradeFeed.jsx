import { qtyText } from "../../lib/replay.js";
import { shortDate, signed, tone } from "../../lib/format.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

export default function TradeFeed({ result, upto, onSelect }) {
  const rows = [];
  for (let k = 0; k <= upto; k++) for (const [j, e] of result.events[k].entries()) rows.push({ k, j, e });
  rows.reverse();
  const paged = usePaged(rows, 15);
  if (!rows.length) return <div className="none">No trades yet.</div>;
  return (
    <>
    <div className="feed">
      {paged.rows.map(({ k, j, e }) => (
        <div className="trade clickable" key={`${k}-${j}`} role="button" tabIndex={0} title="Show this trade on the chart" onClick={() => onSelect?.({ sym: e.sym, at: k })} onKeyDown={(ev) => ev.key === "Enter" && onSelect?.({ sym: e.sym, at: k })}>
          <span className="dt">{shortDate(result.days[k])}</span>
          <span className={`pill ${e.t}${e.t === "sell" && e.pnl > 0 ? " win" : ""}`}>{e.t === "buy" ? (e.add ? "ADD" : "BUY") : "SELL"}</span>
          <div>
            <div><b>{e.sym}</b> {qtyText(result, e.sym, e.q)} at {e.px.toFixed(2)}{e.t === "sell" && ` (bought ${e.entry.toFixed(2)})`}</div>
            <div className="why">
              {e.t === "buy" ? e.note : `${e.why}, held ${e.days} days, fees ${Math.round(e.fees)}${e.slip != null ? `, slippage ${Math.round(e.slip)}` : ""}`}
            </div>
          </div>
          {e.t === "sell" ? <span className={`pnl ${tone(e.pnl)}`}>{signed(e.pnl)}</span> : <span />}
        </div>
      ))}
    </div>
    <Pager paged={paged} />
    </>
  );
}
