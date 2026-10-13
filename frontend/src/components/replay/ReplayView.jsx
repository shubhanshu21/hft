import Panel from "../ui/Panel.jsx";
import EquityLine from "../charts/EquityLine.jsx";
import PlayerControls from "./PlayerControls.jsx";
import StatsStrip from "./StatsStrip.jsx";
import PriceChartPanel from "./PriceChartPanel.jsx";
import HoldingsList from "./HoldingsList.jsx";
import TradeFeed from "./TradeFeed.jsx";
import MetricsGrid from "../backtest/MetricsGrid.jsx";
import { usePlayer } from "../../hooks/usePlayer.js";
import { inr, longDate, pct, signed, tone } from "../../lib/format.js";
import { tally } from "../../lib/replay.js";

export default function ReplayView({ result, title, blurb, live = false }) {
  const count = result.days.length;
  const player = usePlayer(count, result.events);
  if (live) player.cur = count - 1;                     // a run in progress: always show the newest day
  const start = result.metrics?.start_capital ?? result.equity[0];
  const eq = result.equity[player.cur], change = eq - start;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{title ?? "Backtest replay"}</h1>
          {blurb && <div className="sub">{blurb}</div>}
        </div>
        <div className="big">
          <div className="date">{live ? "Running: " : ""}{longDate(result.days[player.cur])}</div>
          <div className="eq">{inr(eq)}</div>
          <div className="chg"><span className={tone(change)}>{signed(change)} ({pct(change / start * 100)})</span> since start</div>
        </div>
      </div>
      {result.metrics && <MetricsGrid metrics={result.metrics} />}
      {!live && <PlayerControls player={{ ...player, count }} />}
      <StatsStrip totals={tally(result, player.cur)} open={result.pos[player.cur].length} />
      <div className="replay-grid">
        <div className="replay-main">
          <PriceChartPanel result={result} at={player.cur} />
          <Panel title="Account value"><EquityLine result={result} upto={player.cur} start={start} /></Panel>
        </div>
        <aside className="replay-side">
          <Panel title="Holding now"><HoldingsList result={result} at={player.cur} /></Panel>
          <Panel title="Trades as they happened"><TradeFeed result={result} upto={player.cur} /></Panel>
        </aside>
      </div>
    </>
  );
}
