import { useState } from "react";
import Panel from "../ui/Panel.jsx";
import EquityLine from "../charts/EquityLine.jsx";
import DrawdownChart from "../charts/DrawdownChart.jsx";
import PlayerControls from "./PlayerControls.jsx";
import PriceChartPanel from "./PriceChartPanel.jsx";
import HoldingsList from "./HoldingsList.jsx";
import TradeFeed from "./TradeFeed.jsx";
import MetricsGrid from "../backtest/MetricsGrid.jsx";
import Icon from "../ui/Icon.jsx";
import Sparkline from "../ui/Sparkline.jsx";
import SettingsChips from "../analysis/SettingsChips.jsx";
import MonthlyGrid from "../analysis/MonthlyGrid.jsx";
import YearTable from "../analysis/YearTable.jsx";
import StockTable from "../analysis/StockTable.jsx";
import { usePlayer } from "../../hooks/usePlayer.js";
import { inr, longDate, pct, signed, tone } from "../../lib/format.js";
import { tally, unitNoun } from "../../lib/replay.js";
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
      <section className="verdict">
        <div className="v-main">
          <div className="v-eyebrow"><Icon name="chart" size={15} /> {title ?? "Backtest replay"}{blurb && <span className="v-blurb">{blurb}</span>}</div>
          <div className="v-date">{live ? "Running: " : ""}{longDate(result.days[player.cur])}</div>
          <div className="v-big">{inr(eq)}</div>
          <div className="v-chg"><span className={`v-delta ${change >= 0 ? "is-up" : "is-down"}`}>{change >= 0 ? "▲" : "▼"} {signed(change)} ({pct(change / start * 100)})</span> since {inr(start)}</div>
          {nOpen > 0 && Math.abs(openPnl) >= 1 && (
            <div className="v-note">= {signed(totals.profit)} from sold trades {openPnl >= 0 ? "+" : "−"} {inr(Math.abs(openPnl))} {openPnl >= 0 ? "gain" : "loss"} on {nOpen} open trade{nOpen > 1 ? "s" : ""} (not sold yet)</div>
          )}
          {bench != null && (() => {
            const b = (bench / start - 1) * 100, gap = change / start * 100 - b;
            return <div className={`v-pill ${gap >= 0 ? "is-up" : "is-down"}`}>{gap >= 0 ? "Beat" : "Behind"} {result.benchmark_label ?? "buy and hold"} ({pct(b)}) by {Math.abs(gap).toFixed(1)} points</div>;
          })()}
          {!live && result.metrics?.final_equity != null && (() => {
            const m = result.metrics, gain = m.final_equity >= m.start_capital;
            const taxed = m.final_pretax != null && Math.abs(m.final_pretax - m.final_equity) > 0.5;
            return (
              <p className="story">
                <b>{inr(m.start_capital)}</b> {gain ? "grew to" : "fell to"} <b>{inr(m.final_equity)}</b> after fees{taxed ? " and tax" : ""} between {longDate(result.days[0])} and {longDate(result.days[count - 1])}
                {m.max_drawdown_pct < 0 ? <> — on the way it dropped as much as <b>{Math.abs(m.max_drawdown_pct).toFixed(1)}%</b> below its high point.</> : "."}
              </p>
            );
          })()}
        </div>
        <div className="v-chart">
          <Sparkline values={result.equity.slice(0, player.cur + 1)} bench={result.benchmark ? result.benchmark.slice(0, player.cur + 1) : null} baseline={start} height={190} />
          <div className="v-chart-key"><span><i className="k-line" /> account value</span>{result.benchmark && <span><i className="k-dash" /> {result.benchmark_label ?? "buy and hold"}</span>}<span><i className="k-base" /> starting money</span></div>
        </div>
        <details className="v-settings"><summary>Settings used for this run</summary><SettingsChips params={params} /></details>
      </section>
      {result.metrics && (() => {
        const sells = result.events.flatMap((ev, k) => ev.filter((e) => e.t === "sell" && e.pnl != null).map((e) => ({ sym: e.sym, pnl: e.pnl, day: result.days[k] })));
        const by = (f) => sells.length ? sells.reduce((a, b) => (f(b.pnl, a.pnl) ? b : a)) : null;
        return <MetricsGrid metrics={result.metrics} equity={result.equity} best={by((x, y) => x > y)} worst={by((x, y) => x < y)} />;
      })()}
      {!live && <PlayerControls sticky player={{ ...player, count }} summary={<><span>Up to <b>{longDate(result.days[player.cur])}</b></span><span><b>{totals.n}</b> trades · <b>{totals.wins}</b> won</span><span>Profit so far (before tax) <b className={tone(totals.profit)}>{signed(totals.profit)}</b></span><span>Fees + funding <b>{inr(totals.fees)}</b></span><span><b>{nOpen}</b> open now</span></>} />}
      <div className="replay-grid">
        <div className="replay-main">
          <div id="price-chart"><PriceChartPanel result={result} at={player.cur} focus={focus} onSelect={select} controls={!live && <PlayerControls player={{ ...player, count }} idp="fs-" />} /></div>
          <Panel title={`Account value (dashed line: ${result.benchmark_label ?? `buying every ${unitNoun(result)} and holding`})`}><EquityLine result={result} upto={player.cur} start={start} /></Panel>
          <Panel title="Drop from the highest account value"><DrawdownChart result={result} upto={player.cur} /></Panel>
        </div>
        <aside className="replay-side">
          <Panel title="Holding now"><HoldingsList result={result} at={player.cur} /></Panel>
          <Panel title="Trades (click one to see it on the chart)"><TradeFeed result={result} upto={player.cur} onSelect={select} />{!live && <button type="button" className="csv-btn" onClick={() => download(fileName, tradesCsv(result))}>Download all trades (CSV)</button>}</Panel>
        </aside>
      </div>
      {!live && (
        <div className="analysis-grid">
          <div className="stack"><YearTable result={result} start={start} />
          <MonthlyGrid result={result} start={start} /></div>
          <StockTable result={result} onPick={(sym) => select({ sym, at: -1 })} />
        </div>
      )}
    </>
  );
}
