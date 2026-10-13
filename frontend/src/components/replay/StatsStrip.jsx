import Panel from "../ui/Panel.jsx";
import Stat from "../ui/Stat.jsx";
import { inr, signed, tone } from "../../lib/format.js";

export default function StatsStrip({ totals, open }) {
  return (
    <Panel className="stats">
      <Stat label="Closed trades" value={totals.n} />
      <Stat label="Winners" value={totals.n ? `${totals.wins} of ${totals.n}` : "-"} />
      <Stat label="Profit after fees and slippage" value={<span className={tone(totals.profit)}>{signed(totals.profit)}</span>} />
      <Stat label="Fees and taxes paid" value={inr(totals.fees)} />
      <Stat label="Lost to slippage" value={inr(totals.slippage)} />
      <Stat label="Open now" value={open} />
    </Panel>
  );
}
