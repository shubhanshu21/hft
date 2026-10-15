import { inr, signed, tone } from "../../lib/format.js";

const time = (s) => (s ? String(s).replace("T", " ").slice(11, 16) : "");
const day = (s) => (s ? new Date(s).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "");
const px = (v, d) => (v == null ? "-" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));

/** One open paper trade: live price, profit now, what selling now would leave after fees, and where the price sits between the stop and an
 *  equal distance above the buy (below it for a short). */
export default function OpenTradeCard({ p }) {
  const d = 2;
  const long = p.direction !== "short";
  const size = p.market === "currency" ? `${p.qty} lot${p.qty === 1 ? "" : "s"} · ${p.units.toLocaleString("en-IN")} ${p.symbol.slice(0, 3)}` : `${p.qty.toLocaleString("en-IN")} shares`;
  const dist = Math.abs(p.entry_price - p.current_stop) || 1;
  const pos = p.price_now == null ? null : Math.max(0, Math.min(100, 50 + ((p.price_now - p.entry_price) / dist) * 50 * (long ? 1 : -1)));
  return (
    <article className="trade-card">
      <header>
        <div>
          <div className="tc-sym">{p.symbol} <span className={`pill ${long ? "buy" : "sell"}`}>{long ? "BUY" : "SELL"}</span></div>
          <div className="note">{p.strategy} · {size} · since {day(p.entry_time)} {time(p.entry_time)}</div>
        </div>
        <div className="tc-pnl">
          <div className={`tc-big ${p.profit_now != null ? tone(p.profit_now) : ""}`}>{p.profit_now != null ? signed(p.profit_now) : "-"}</div>
          <div className="note">profit now, before fees</div>
        </div>
      </header>
      <div className="tc-bar" aria-label="Price between the stop and the buy price">
        <div className="tc-track"><span className="tc-mid" />{pos != null && <span className={`tc-dot ${tone(p.profit_now)}`} style={{ left: `${pos}%` }} />}</div>
        <div className="tc-ends"><span>stop {px(p.current_stop, d)}</span><span>bought {px(p.entry_price, d)}</span><span /></div>
      </div>
      <dl className="tc-grid">
        <div><dt>Last traded price</dt><dd>{px(p.price_now, d)} <span className="note">{p.price_source === "live" ? `traded ${time(p.price_time)}` : p.price_source ? `close ${p.price_time}` : ""}</span></dd></div>
        <div><dt>Buyers / sellers now</dt><dd>{p.bid != null || p.ask != null ? `${px(p.bid, d)} / ${px(p.ask, d)}` : "-"} <span className="note">{p.checked_at ? `checked ${time(p.checked_at)}` : ""}</span></dd></div>
        <div><dt>Move</dt><dd className={p.move_pct != null ? tone(p.move_pct) : ""}>{p.move_pct != null ? `${p.move_pct >= 0 ? "+" : ""}${p.move_pct.toFixed(2)}%` : "-"}</dd></div>
        <div><dt>If sold now, after fees</dt><dd className={p.net_if_sold != null ? tone(p.net_if_sold) : ""}>{p.net_if_sold != null ? signed(p.net_if_sold) : "-"}</dd></div>
        <div><dt>Fees to sell now</dt><dd>{p.fees_if_sold != null ? inr(p.fees_if_sold) : "-"}</dd></div>
        <div><dt>Loss if stop is hit</dt><dd className="down">{inr(-Math.abs(p.risk_to_stop))}</dd></div>
        <div><dt>{p.market === "currency" ? "Margin blocked" : "Money in it"}</dt><dd>{inr(p.margin_used)}</dd></div>
      </dl>
    </article>
  );
}
