import { useState } from "react";
import Panel from "../ui/Panel.jsx";
import EquityLine from "../charts/EquityLine.jsx";
import DrawdownChart from "../charts/DrawdownChart.jsx";
import PlayerControls from "./PlayerControls.jsx";
import StatsStrip from "./StatsStrip.jsx";
import PriceChartPanel from "./PriceChartPanel.jsx";
import HoldingsList from "./HoldingsList.jsx";
import TradeFeed from "./TradeFeed.jsx";
import MetricsGrid from "../backtest/MetricsGrid.jsx";
import SettingsChips from "../analysis/SettingsChips.jsx";
import MonthlyGrid from "../analysis/MonthlyGrid.jsx";
import StockTable from "../analysis/StockTable.jsx";
import { usePlayer } from "../../hooks/usePlayer.js";
import { inr, longDate, pct, signed, tone } from "../../lib/format.js";
import { tally } from "../../lib/replay.js";
import { download, tradesCsv } from "../../lib/analysis.js";

export default function ReplayView({ result, title, blurb, params, live = false, fileName = "trades.csv" }) {
  const count = result.days.length;
  const player = usePlayer(count, result.events);
  if (live) player.cur = count - 1;                     // a run in progress: always show the newest day
  const [focus, setFocus] = useState(null);             // { sym, at, token }: a clicked trade or stock, shown on the chart
  const select = (f) => { setFocus({ ...f, token: Date.now() }); document.getElementById("price-chart")?.scrollIntoView({ behavior: "smooth", block: "start" }); };
  const start = result.metrics?.start_capital ?? result.equity[0];
  const eq = result.equity[player.cur], change = eq - start;
  const bench = result.benchmark?.[player.cur];
  const totals = tally(result, player.cur), nOpen = result.pos[player.cur].length;
  const openPnl = change - totals.profit;                 // what the still-open trades are worth today versus their buy price, after costs so far
  return (
    <>
      <section className="result-hero">
        <div className="hero-left">
          <h1>{title ?? "Backtest replay"}</h1>
          {blurb && <div className="sub">{blurb}</div>}
          <SettingsChips params={params} />
        </div>
        <div className="big">
          <div className="date">{live ? "Running: " : ""}{longDate(result.days[player.cur])}</div>
          <div className="eq">{inr(eq)}</div>
          <div className="chg"><span className={tone(change)}>{signed(change)} ({pct(change / start * 100)})</span> since {inr(start)}</div>
          {nOpen > 0 && Math.abs(openPnl) >= 1 && (
            <div className="chg muted">= {signed(totals.profit)} from sold trades {openPnl >= 0 ? "+" : "−"} {inr(Math.abs(openPnl))} {openPnl >= 0 ? "gain" : "loss"} on {nOpen} open trade{nOpen > 1 ? "s" : ""} (not sold yet)</div>
          )}
          {bench != null && (() => {
            const b = (bench / start - 1) * 100, gap = change / start * 100 - b;
            return <div className={`vs ${gap >= 0 ? "up" : "down"}`}>{gap >= 0 ? "Beat" : "Behind"} buy and hold ({pct(b)}) by {Math.abs(gap).toFixed(1)} points</div>;
          })()}
        </div>
      </section>
      {result.metrics && <MetricsGrid metrics={result.metrics} />}
      {!live && <PlayerControls player={{ ...player, count }} />}
      <StatsStrip totals={totals} open={nOpen} />
      <div className="replay-grid">
        <div className="replay-main">
          <div id="price-chart"><PriceChartPanel result={result} at={player.cur} focus={focus} onSelect={select} controls={!live && <PlayerControls player={{ ...player, count }} idp="fs-" />} /></div>
          <Panel title={`Account value (dashed line: buying every ${result.lots ? "commodity" : "stock"} and holding)`}><EquityLine result={result} upto={player.cur} start={start} /></Panel>
          <Panel title="Drop from the highest account value"><DrawdownChart result={result} upto={player.cur} /></Panel>
        </div>
        <aside className="replay-side">
          <Panel title="Holding now"><HoldingsList result={result} at={player.cur} /></Panel>
          <Panel title="Trades as they happened (click one to see it)"><TradeFeed result={result} upto={player.cur} onSelect={select} /></Panel>
        </aside>
      </div>
      {!live && (
        <div className="analysis-grid">
          <MonthlyGrid result={result} start={start} />
          <StockTable result={result} onPick={(sym) => select({ sym, at: -1 })} />
        </div>
      )}
      {!live && <div><button type="button" onClick={() => download(fileName, tradesCsv(result))}>Download trades (CSV)</button></div>}
    </>
  );
}
