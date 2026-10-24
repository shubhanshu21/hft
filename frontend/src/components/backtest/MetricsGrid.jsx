import Stat from "../ui/Stat.jsx";
import { inr, pct, signed, tone } from "../../lib/format.js";

/** Start -> trades -> fees -> (tax) -> what you keep, one line each, in plain words (2026-10-24: the old "Costs" card mixed
 *  before-tax and after-tax numbers and confused the user). Crypto runs carry final_pretax; the tax is the gap to final_equity. */
export function moneyFlow(m) {
  if (m.gross_profit == null || m.start_capital == null) return [["You keep", inr(m.final_equity)]];
  const slip = m.slippage ?? 0;
  const pretax = m.final_pretax ?? m.final_equity;
  const fees = m.final_pretax != null ? m.gross_profit - (pretax - m.start_capital) - slip : (m.fees ?? 0);
  const tax = pretax - m.final_equity;
  return [
    ["You started with", inr(m.start_capital)],
    ["Profit from price moves (before fees)", <span className={tone(m.gross_profit)}>{signed(m.gross_profit)}</span>],
    ["Fees + funding paid", <span className="down">{signed(-fees)}</span>],
    ...(slip > 0.5 ? [["Slippage", <span className="down">{signed(-slip)}</span>]] : []),
    ...(tax > 0.5 ? [["Your money before income tax", inr(pretax)], ["Income tax", <span className="down">{signed(-tax)}</span>]] : []),
    ["You keep", <b>{inr(m.final_equity)}</b>],
  ];
}

/** The full-run numbers, shown before the replay so the verdict is visible first: outcome, trades and costs as three cards. */
export default function MetricsGrid({ metrics: m }) {
  const pf = m.profit_factor == null ? "-" : m.profit_factor === Infinity || m.profit_factor === "Infinity" ? "no losers" : m.profit_factor;
  const groups = [
    ["Outcome", [
      ["You keep", inr(m.final_equity)],
      [m.final_pretax != null && m.final_pretax !== m.final_equity ? "Return after tax" : "Return", <span className={tone(m.return_pct)}>{pct(m.return_pct, 1)}</span>],
      ["Per year (CAGR)", <span className={tone(m.cagr_pct)}>{pct(m.cagr_pct, 1)}</span>],
      ["Worst fall", <span className={m.max_drawdown_pct < 0 ? "down" : ""}>{m.max_drawdown_pct}%</span>],
    ]],
    ["Trades", [
      ["Closed trades", m.trades],
      ["Win rate", m.trades ? <>{m.win_rate_pct}% <span className="v-sub">{m.wins} of {m.trades}</span></> : "-"],
      ["Profit factor", pf],
      ["Average hold", m.avg_hold_days != null ? `${m.avg_hold_days} days` : "-"],
    ]],
    ["Where the money went", moneyFlow(m)],
  ];
  return (
    <section className="metrics-wrap" aria-label="Whole run, after fees and slippage">
      <div className="metrics">
        {groups.map(([title, items]) => (
          <div className="metric-group" key={title}>
            <h2>{title}</h2>
            <div className="metric-grid">{items.map(([l, v]) => <Stat key={l} label={l} value={v} />)}</div>
          </div>
        ))}
      </div>
      {m.note && <p className="callout">{m.note}</p>}
    </section>
  );
}
