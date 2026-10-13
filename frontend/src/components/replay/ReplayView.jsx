import Panel from "../ui/Panel.jsx";
import EquityChart from "../charts/EquityChart.jsx";
import PlayerControls from "./PlayerControls.jsx";
import StatsStrip from "./StatsStrip.jsx";
import PriceChartPanel from "./PriceChartPanel.jsx";
import HoldingsList from "./HoldingsList.jsx";
import TradeFeed from "./TradeFeed.jsx";
import MetricsGrid from "../backtest/MetricsGrid.jsx";
import { usePlayer } from "../../hooks/usePlayer.js";
import { inr, longDate, pct, signed, tone } from "../../lib/format.js";
import { tally } from "../../lib/replay.js";

export default function ReplayView({ result, title, blurb }) {
  const count = result.days.length;
  const player = usePlayer(count);
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
          <div className="date">{longDate(result.days[player.cur])}</div>
          <div className="eq">{inr(eq)}</div>
          <div className="chg"><span className={tone(change)}>{signed(change)} ({pct(change / start * 100)})</span> since start</div>
        </div>
      </div>
      {result.metrics && <MetricsGrid metrics={result.metrics} />}
      <PlayerControls player={{ ...player, count }} />
      <StatsStrip totals={tally(result, player.cur)} open={result.pos[player.cur].length} />
      <PriceChartPanel result={result} at={player.cur} />
      <Panel title="Account value"><EquityChart result={result} upto={player.cur} start={start} /></Panel>
      <div className="cols">
        <Panel title="Holding now"><HoldingsList result={result} at={player.cur} /></Panel>
        <Panel title="Trades as they happened"><TradeFeed result={result} upto={player.cur} /></Panel>
      </div>
    </>
  );
}
