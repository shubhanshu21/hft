import { qtyText } from "../../lib/replay.js";
import MiniCandles from "../charts/MiniCandles.jsx";
import { signed, tone, price, qty } from "../../lib/format.js";

export default function HoldingsList({ result, at }) {
  const held = result.pos[at];
  if (!held.length) return <div className="none">Holding only cash.</div>;
  return held.map((o) => {
    const pl = (o.px - o.e) * o.q;
    return (
      <div className="pos" key={o.sym}>
        <div>
          <div className="s">{o.sym}</div>
          <div className="d">{result.lots ? qtyText(result, o.sym, o.q) : `${qty(o.q)} units`}, bought at {price(o.e)}, now {price(o.px)}</div>
          <div className={`p ${tone(pl)}`}>{signed(pl)} ({((o.px / o.e - 1) * 100).toFixed(1)}%) before fees</div>
        </div>
        {result.bars?.[o.sym] && <MiniCandles bars={result.bars[o.sym]} from={o.start} to={at} entry={o.e} />}
      </div>
    );
  });
}
