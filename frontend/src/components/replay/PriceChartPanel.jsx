import { useEffect, useState } from "react";

const VOL_KEY = "hft.showVolume";
const readVol = () => { try { return localStorage.getItem(VOL_KEY) !== "0"; } catch { return true; } };
import Panel from "../ui/Panel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import SymbolTrades from "./SymbolTrades.jsx";
import { followedSymbol, symbolsWithBars, tradesBySymbol } from "../../lib/replay.js";
import { signed, tone } from "../../lib/format.js";

/** Candlesticks of one symbol with all of the strategy's trades in it, plus a chip per symbol (trade count and profit) to switch. */
export default function PriceChartPanel({ result, at, focus, onSelect }) {
  const [picked, setPicked] = useState("");
  const [fitKey, setFitKey] = useState(0);
  useEffect(() => { if (focus?.sym) setPicked(focus.sym); }, [focus?.token]);
  const [showVolume, setShowVolume] = useState(readVol);
  const toggleVolume = (on) => { setShowVolume(on); try { localStorage.setItem(VOL_KEY, on ? "1" : "0"); } catch { /* not remembered */ } };
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
          <label className="note check" htmlFor="showvol"><input id="showvol" type="checkbox" checked={showVolume} onChange={(e) => toggleVolume(e.target.checked)} /> Volume</label>
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
      <PriceChart symbol={sym} bars={result.bars[sym]} calendar={calendar} first={0} last={last} at={at} trades={mine} fitKey={fitKey} showVolume={showVolume} focus={focus && focus.sym === sym ? focus : null} />
      <SymbolTrades trades={mine} days={result.days} onSelect={(t) => onSelect?.({ sym, at: t.i0 })} />
    </Panel>
  );
}
