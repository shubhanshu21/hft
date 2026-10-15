import Stat from "../ui/Stat.jsx";
import { inr, pct, signed, tone } from "../../lib/format.js";

/** The full-run numbers, shown before the replay so the verdict is visible first: outcome, trades and costs as three cards. */
export default function MetricsGrid({ metrics: m }) {
  const pf = m.profit_factor == null ? "-" : m.profit_factor === Infinity || m.profit_factor === "Infinity" ? "no losers" : m.profit_factor;
  const groups = [
    ["Outcome", [
      ["Final account", inr(m.final_equity)],
      ["Return", <span className={tone(m.return_pct)}>{pct(m.return_pct, 1)}</span>],
      ["Per year (CAGR)", <span className={tone(m.cagr_pct)}>{pct(m.cagr_pct, 1)}</span>],
      ["Worst fall", <span className={m.max_drawdown_pct < 0 ? "down" : ""}>{m.max_drawdown_pct}%</span>],
    ]],
    ["Trades", [
      ["Closed trades", m.trades],
      ["Win rate", m.trades ? <>{m.win_rate_pct}% <span className="v-sub">{m.wins} of {m.trades}</span></> : "-"],
      ["Profit factor", pf],
      ["Average hold", m.avg_hold_days != null ? `${m.avg_hold_days} days` : "-"],
    ]],
    ["Costs", [
      ["Before fees and slippage", m.gross_profit != null ? <span className={tone(m.gross_profit)}>{signed(m.gross_profit)}</span> : "-"],
      ["Fees and taxes", m.fees != null ? inr(m.fees) : "-"],
      ["Slippage", m.slippage != null ? inr(m.slippage) : "-"],
      ["After costs (sold trades)", m.closed_profit != null ? <span className={tone(m.closed_profit)}>{signed(m.closed_profit)}</span> : "-"],
      ...(m.open_at_end ? [[`Not sold yet (${m.open_at_end} open)`, <span className={tone(m.open_profit)}>{signed(m.open_profit)}</span>]] : []),
    ]],
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
