import { useState } from "react";
import Panel from "../ui/Panel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import SymbolTrades from "./SymbolTrades.jsx";
import { followedSymbol, symbolsWithBars, tradesBySymbol } from "../../lib/replay.js";
import { signed, tone } from "../../lib/format.js";

/** Candlesticks of one symbol with all of the strategy's trades in it, plus a chip per symbol (trade count and profit) to switch. */
export default function PriceChartPanel({ result, at }) {
  const [picked, setPicked] = useState("");
  const [fitKey, setFitKey] = useState(0);
  const syms = symbolsWithBars(result);
  if (!syms.length) return null;

  const trades = tradesBySymbol(result, at);
  const sym = picked || followedSymbol(result, at);
  const mine = trades[sym] ?? [];
  const calendar = result.days_all ?? result.days;
  const last = calendar.length - 1;
  const sorted = [...syms].sort((a, b) => (trades[b]?.length ?? 0) - (trades[a]?.length ?? 0));

  return (
    <Panel>
      <div className="chart-head">
        <h2>{sym}: price and every trade taken <span className="hint-inline">drag to move, scroll to zoom</span></h2>
        <div className="row-flex">
          <span className="legend"><span className="acc">&#9650; buy</span><span className="up">&#9660; sell, profit</span><span className="down">&#9660; sell, loss</span></span>
          <button type="button" onClick={() => setFitKey(fitKey + 1)}>Reset zoom</button>
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
      <PriceChart symbol={sym} bars={result.bars[sym]} calendar={calendar} first={0} last={last} at={at} trades={mine} fitKey={fitKey} />
      <SymbolTrades trades={mine} days={result.days} />
    </Panel>
  );
}
