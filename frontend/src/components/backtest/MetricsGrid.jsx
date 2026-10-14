import Panel from "../ui/Panel.jsx";
import Stat from "../ui/Stat.jsx";
import { inr, pct, signed, tone } from "../../lib/format.js";

/** The full-run numbers, shown before the replay so the verdict is visible first. */
export default function MetricsGrid({ metrics: m }) {
  const pf = m.profit_factor == null ? "-" : m.profit_factor === Infinity || m.profit_factor === "Infinity" ? "no losers" : m.profit_factor;
  return (
    <Panel title="Whole run, after fees and slippage">
      <div className="cost-table">
        <Stat label="Final account" value={inr(m.final_equity)} />
        <Stat label="Return" value={<span className={tone(m.return_pct)}>{pct(m.return_pct, 2)}</span>} />
        <Stat label="Per year (CAGR)" value={pct(m.cagr_pct, 1)} />
        <Stat label="Worst fall" value={`${m.max_drawdown_pct}%`} />
        <Stat label="Trades" value={m.trades} />
        <Stat label="Win rate" value={m.trades ? `${m.win_rate_pct}%` : "-"} />
        <Stat label="Profit factor" value={pf} />
        {m.closed_profit != null && <Stat label="Profit on sold trades" value={<span className={tone(m.closed_profit)}>{signed(m.closed_profit)}</span>} />}
        {m.open_profit != null && <Stat label={`Not sold yet (${m.open_at_end} open)`} value={<span className={tone(m.open_profit)}>{signed(m.open_profit)}</span>} />}
        {m.gross_profit != null && <Stat label="Sold trades before fees and slippage" value={<span className={tone(m.gross_profit)}>{signed(m.gross_profit)}</span>} />}
        {m.fees != null && <Stat label="Fees and taxes" value={inr(m.fees)} />}
        {m.slippage != null && <Stat label="Slippage" value={inr(m.slippage)} />}
        {m.avg_hold_days != null && <Stat label="Average hold" value={`${m.avg_hold_days} days`} />}
      </div>
      {m.note && <p className="note">{m.note}</p>}
    </Panel>
  );
}
