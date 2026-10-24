import Panel from "../ui/Panel.jsx";
import Stat from "../ui/Stat.jsx";
import { inr, signed, tone } from "../../lib/format.js";

export default function StatsStrip({ totals, open }) {
  return (
    <Panel className="stats">
      <Stat label="Closed trades" value={totals.n} />
      <Stat label="Winners" value={totals.n ? `${totals.wins} of ${totals.n}` : "-"} />
      <Stat label="Profit so far (before tax)" value={<span className={tone(totals.profit)}>{signed(totals.profit)}</span>} />
      <Stat label="Fees + funding so far" value={inr(totals.fees)} />
      {totals.slippage > 0.5 && <Stat label="Slippage so far" value={inr(totals.slippage)} />}
      <Stat label="Open now" value={open} />
    </Panel>
  );
}
