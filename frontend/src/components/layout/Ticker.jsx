import { useOverview } from "../../hooks/useOverview.js";
import { tickerItems } from "../../lib/overview.js";
import { inr, signed } from "../../lib/format.js";

/** The scrolling price tape (Look B): total, each paper account, each open trade. Pauses on hover. */
export default function Ticker() {
  const { data } = useOverview();
  const items = tickerItems(data);
  if (!items.length) return <div className="tape" aria-hidden="true"><div className="tape-track"><span>Loading prices…</span></div></div>;
  const one = items.map((x, i) => (
    <span key={i}><b>{x.label}</b>{x.value != null && <>{inr(x.value)} </>}
      {x.change != null && <span className={x.change >= 0 ? "t-up" : "t-down"}>{x.kind === "pct" ? `${x.change >= 0 ? "+" : ""}${x.change.toFixed(1)}%` : signed(x.change)}</span>}</span>
  ));
  return <div className="tape" role="marquee" aria-label="Paper accounts and open trades"><div className="tape-track">{one}{one}{one}{one}</div></div>;
}
