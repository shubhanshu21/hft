import { useEffect, useRef, useState } from "react";

const VOL_KEY = "hft.showVolume";
const readVol = () => { try { return localStorage.getItem(VOL_KEY) !== "0"; } catch { return true; } };
import Panel from "../ui/Panel.jsx";
import PriceChart from "../charts/PriceChart.jsx";
import SymbolTrades from "./SymbolTrades.jsx";
import { qtyText, followedSymbol, symbolsWithBars, tradesBySymbol } from "../../lib/replay.js";
import { signed, tone } from "../../lib/format.js";

/** Candlesticks of one symbol with all of the strategy's trades in it, plus a chip per symbol (trade count and profit) to switch. */
export default function PriceChartPanel({ result, at, focus, onSelect, controls = null }) {
  const [picked, setPicked] = useState("");
  const [fitKey, setFitKey] = useState(0);
  useEffect(() => { if (focus?.sym) setPicked(focus.sym); }, [focus?.token]);
  const [showVolume, setShowVolume] = useState(readVol);
  const fsBox = useRef(null);
  const [full, setFull] = useState(false);
  useEffect(() => {
    const on = () => setFull(document.fullscreenElement === fsBox.current);
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, []);
  const toggleFull = () => (full ? document.exitFullscreen() : fsBox.current?.requestFullscreen?.())?.catch?.(() => {});
  const toggleVolume = (on) => { setShowVolume(on); try { localStorage.setItem(VOL_KEY, on ? "1" : "0"); } catch { /* not remembered */ } };
  const syms = symbolsWithBars(result);
  if (!syms.length) return null;

  const trades = tradesBySymbol(result, at);
  const sym = (picked && syms.includes(picked)) ? picked : (followedSymbol(result, at) || syms[0]);
  const mine = trades[sym] ?? [];
  const calendar = result.days_all ?? result.days;
  const last = calendar.length - 1;
  const stopPath = (() => {                                         // the stop level of the trade held in this stock, day by day (breakout / trend strategies)
    const byDay = new Map();
    (result.pos ?? []).forEach((day, i) => { const p = day.find((x) => x.sym === sym); if (p?.stop != null) byDay.set(result.days[i], p.stop); });
    return byDay.size ? calendar.map((d) => byDay.get(d) ?? null) : null;
  })();
  const c4 = result.chart;                                           // a 4-hour strategy brings its own 4-hour candles and trade positions
  const at4 = c4 ? c4.day_end[at] : at;
  const mine4 = c4 ? (c4.trades[sym] ?? []).filter((t) => t.i0 != null && t.i0 <= at4).map((t) => (t.i1 != null && t.i1 > at4 ? { ...t, i1: null, pnl: null, exit: null } : t)) : null;
  const sorted = [...syms].sort((a, b) => (trades[b]?.length ?? 0) - (trades[a]?.length ?? 0));

  return (
    <Panel>
      <div ref={fsBox} className="chart-fs">
      <div className="chart-head">
        <h2>{sym} · {c4 ? "4-hour" : result.lots ? "monthly" : "daily"} candles: price and every trade taken <span className="hint-inline">drag to move, scroll to zoom</span></h2>
        <div className="row-flex">
          <span className="legend"><span className="acc">&#9650; buy</span>{c4 && <span className="acc">&#9660; short</span>}<span className="up">{c4 ? "sell / cover, profit" : "▼ sell, profit"}</span><span className="down">{c4 ? "sell / cover, loss" : "▼ sell, loss"}</span></span>
          {!c4 && <label className="note check" htmlFor="showvol"><input id="showvol" type="checkbox" checked={showVolume} onChange={(e) => toggleVolume(e.target.checked)} /> Volume</label>}
          <button type="button" onClick={() => setFitKey(fitKey + 1)}>Reset zoom</button>
          <button type="button" onClick={toggleFull} title="Full screen (Esc to leave)">{full ? "Exit full screen" : "Full screen"}</button>
        </div>
      </div>
      {full && controls}                                  {/* the Play controls, only in full screen (the page has its own above) */}
      <div className="chips" role="group" aria-label="Choose what to show on the chart">
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
      {c4 ? (
        <PriceChart qtyFmt={(q) => qtyText(result, sym, q, true)} tf={c4.tf} overlay={null} stopPath={null} symbol={sym} bars={c4.bars[sym]} calendar={c4.times} first={0} last={c4.times.length - 1} at={at4} trades={mine4} fitKey={fitKey} showVolume={false} focus={null} />
      ) : (
        <PriceChart qtyFmt={(q) => qtyText(result, sym, q, true)} tf={result.lots ? "1M" : "1D"} overlay={result.overlays?.[sym] ?? null} stopPath={stopPath} symbol={sym} bars={result.bars[sym]} calendar={calendar} first={0} last={last} at={at} trades={mine} fitKey={fitKey} showVolume={showVolume} focus={focus && focus.sym === sym ? focus : null} />
      )}
      <SymbolTrades result={result} trades={mine} days={result.days} onSelect={(t) => onSelect?.({ sym, at: t.i0 })} />
      </div>
    </Panel>
  );
}
