import { heldText, qtyText } from "../../lib/replay.js";
import { inr, price, shortDate, signed, tone, whyText } from "../../lib/format.js";
import Pager from "../ui/Pager.jsx";
import { usePaged } from "../../hooks/usePaged.js";

/** Every trade as ONE row (2026-10-24 -- it used to be a buy row and a sell row, 218 rows for 109 trades): newest first, open trades at
 *  the top. Click a row to see the trade on the chart. */
export default function TradeFeed({ result, upto, onSelect }) {
  const open = {}, rows = [];
  for (let k = 0; k <= upto; k++) for (const e of result.events[k]) {
    if (e.t === "buy" && !open[e.sym]) open[e.sym] = { buy: e, i0: k };
    else if (e.t === "sell" && open[e.sym]) { rows.push({ ...open[e.sym], sell: e, i1: k }); delete open[e.sym]; }
  }
  for (const o of Object.values(open)) rows.push({ ...o, sell: null, i1: null });
  rows.sort((a, b) => (b.i1 ?? Infinity) - (a.i1 ?? Infinity) || b.i0 - a.i0);
  const paged = usePaged(rows, 15);
  if (!rows.length) return <div className="none">No trades yet.</div>;
  return (
    <>
      <div className="feed">
        {paged.rows.map(({ buy, sell, i0, i1 }) => {
          const k = i1 ?? i0, win = sell && sell.pnl > 0;
          const go = () => onSelect?.({ sym: buy.sym, at: k });
          return (
            <div className="trade clickable" key={`${buy.sym}-${i0}`} role="button" tabIndex={0} title="Show this trade on the chart" onClick={go} onKeyDown={(ev) => ev.key === "Enter" && go()}>
              <span className="dt">{shortDate(result.days[k])}<br />{String(result.days[k]).slice(0, 4)}</span>
              <span className={`pill ${!sell ? "buy" : win ? "sell win" : "sell"}`}>{!sell ? "OPEN" : win ? "WIN" : "LOSS"}</span>
              <div>
                <div><b>{buy.sym}</b> <span className="muted">{qtyText(result, buy.sym, buy.q)}</span></div>
                <div className="why">
                  {price(buy.px)} → {sell ? price(sell.px) : "still open"}
                  {sell && ` · held ${heldText(sell)} · ${whyText(sell.why)}${sell.fees != null ? ` · fees ${inr(sell.fees)}` : ""}`}
                </div>
              </div>
              {sell ? <span className={`pnl ${tone(sell.pnl)}`}>{signed(sell.pnl)}</span> : <span className="pnl muted">open</span>}
            </div>
          );
        })}
      </div>
      <Pager paged={paged} />
    </>
  );
}
