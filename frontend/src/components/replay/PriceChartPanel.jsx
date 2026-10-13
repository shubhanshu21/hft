import { useState } from "react";
import Panel from "../ui/Panel.jsx";
import CandleChart from "../charts/CandleChart.jsx";
import SymbolTrades from "./SymbolTrades.jsx";
import { followedSymbol, symbolsWithBars, tradesBySymbol } from "../../lib/replay.js";
import { signed, tone } from "../../lib/format.js";

const RANGES = [["all", "Whole run"], ["250", "One year at a time"], ["90", "90 days at a time"]];

/** Candlesticks of one symbol with all of the strategy's trades in it, plus a chip per symbol (trade count and profit) to switch. */
export default function PriceChartPanel({ result, at }) {
  const [picked, setPicked] = useState("");
  const [range, setRange] = useState("all");
  const syms = symbolsWithBars(result);
  if (!syms.length) return null;

  const trades = tradesBySymbol(result, at);
  const sym = picked || followedSymbol(result, at);
  const mine = trades[sym] ?? [];
  // Fixed x-axis, so playback fills the chart left to right. A short range is a page: when the replay passes its end, the next page starts at the left again.
  const n = result.days.length, size = range === "all" ? n : Math.min(Number(range), n);
  const first = range === "all" ? 0 : Math.floor(at / size) * size;
  const last = Math.min(first + size - 1, n - 1);
  const sorted = [...syms].sort((a, b) => (trades[b]?.length ?? 0) - (trades[a]?.length ?? 0));

  return (
    <Panel>
      <div className="chart-head">
        <h2>{sym}: candlesticks and every trade taken</h2>
        <div className="row-flex">
          <span className="legend"><span className="up">&#9650; buy</span><span className="up">&#9660; sell, profit</span><span className="down">&#9660; sell, loss</span></span>
          <label htmlFor="range" className="note">Show</label>
          <select id="range" value={range} onChange={(e) => setRange(e.target.value)}>{RANGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        </div>
      </div>
      <div className="chips" role="group" aria-label="Choose a stock">
        <button type="button" className={`chip ${picked === "" ? "on" : ""}`} onClick={() => setPicked("")}>Follow the trade</button>
        {sorted.map((s) => {
          const t = trades[s] ?? [];
          const p = t.reduce((a, x) => a + (x.pnl ?? 0), 0);
          return (
            <button type="button" key={s} className={`chip ${picked === s ? "on" : ""}`} onClick={() => setPicked(s)}>
              {s} <span className="count">{t.length}</span> <span className={tone(p)}>{t.length ? signed(p) : ""}</span>
            </button>
          );
        })}
      </div>
      <CandleChart symbol={sym} bars={result.bars[sym]} days={result.days} first={first} last={last} at={at} trades={mine} />
      <SymbolTrades trades={mine} days={result.days} />
    </Panel>
  );
}
