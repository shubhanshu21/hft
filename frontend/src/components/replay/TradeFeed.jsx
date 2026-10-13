import { shortDate, signed, tone } from "../../lib/format.js";

export default function TradeFeed({ result, upto }) {
  const rows = [];
  for (let k = 0; k <= upto; k++) for (const [j, e] of result.events[k].entries()) rows.push({ k, j, e });
  rows.reverse();
  if (!rows.length) return <div className="none">No trades yet.</div>;
  return (
    <div className="feed">
      {rows.map(({ k, j, e }) => (
        <div className="trade" key={`${k}-${j}`}>
          <span className="dt">{shortDate(result.days[k])}</span>
          <span className={`pill ${e.t}${e.t === "sell" && e.pnl > 0 ? " win" : ""}`}>{e.t === "buy" ? (e.add ? "ADD" : "BUY") : "SELL"}</span>
          <div>
            <div><b>{e.sym}</b> {e.q} at {e.px.toFixed(2)}{e.t === "sell" && ` (bought ${e.entry.toFixed(2)})`}</div>
            <div className="why">
              {e.t === "buy" ? e.note : `${e.why}, held ${e.days} days, fees ${Math.round(e.fees)}${e.slip != null ? `, slippage ${Math.round(e.slip)}` : ""}`}
            </div>
          </div>
          {e.t === "sell" ? <span className={`pnl ${tone(e.pnl)}`}>{signed(e.pnl)}</span> : <span />}
        </div>
      ))}
    </div>
  );
}
