import { useEffect, useRef, useState } from "react";
import { createChart, LineStyle, LineType } from "lightweight-charts";
import { baseOptions } from "./chartOptions.js";
import { useTheme, withAlpha } from "../../hooks/useTheme.js";
import { signed } from "../../lib/format.js";

/**
 * TradingView-style candlestick chart (lightweight-charts) of one symbol with every trade the strategy took in it:
 * an arrow at each buy and sell, and a line from entry to exit (green = profit, red = loss, dashed = still open).
 * Drag to pan, scroll to zoom, hover for the crosshair. The time axis spans the whole run, so playback fills it from the left.
 * bars[i] = [open, high, low, close, volume?] | null (older results have no volume); showVolume draws volume bars along the bottom.
 * bars[i] = [open, high, low, close] | null; calendar[i] = "YYYY-MM-DD"; trades = [{ i0, i1, entry, exit, pnl, q }].
 */
const OVERLAY_COLORS = ["#d97706", "#7c3aed", "#0284c7"];     // amber, violet, sky: distinct from the buy / profit / loss colours

export default function PriceChart({ qtyFmt = (q) => q ?? "", tf = "1D", overlay = null, stopPath = null, symbol, bars, calendar, first, last, at, trades, fitKey, showVolume = true, focus = null }) {
  const colors = useTheme();
  const box = useRef(null);
  const api = useRef({});
  const lastRange = useRef("");
  const [hover, setHover] = useState(null);

  useEffect(() => {
    const chart = createChart(box.current, baseOptions(colors, 440));
    const candles = chart.addCandlestickSeries({
      upColor: colors.gain, downColor: colors.loss, borderUpColor: colors.gain, borderDownColor: colors.loss, wickUpColor: colors.gain, wickDownColor: colors.loss,
    });
    const volume = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(candles);
      const v = p.seriesData.get(volume);
      setHover(d ? { ...d, volume: v?.value, time: p.time } : null);
    });
    api.current = { chart, candles, volume, lines: [], priceLine: null };
    lastRange.current = "";
    return () => { chart.remove(); api.current = {}; };
  }, [colors, symbol]);

  useEffect(() => {
    const { chart, candles } = api.current;
    if (!chart || !bars) return;
    const data = [];
    for (let i = first; i <= at; i++) if (bars[i]) data.push({ time: calendar[i], open: bars[i][0], high: bars[i][1], low: bars[i][2], close: bars[i][3] });
    candles.setData(data);
    const hasVol = showVolume && bars.some((b) => b && b[4] != null);
    const vols = [];
    if (hasVol) for (let i = first; i <= at; i++) if (bars[i] && bars[i][4] != null) vols.push({ time: calendar[i], value: bars[i][4], color: withAlpha(bars[i][3] >= bars[i][0] ? colors.gain : colors.loss, 0.45) });
    api.current.volume.setData(vols);
    const osc = overlay?.osc;
    if (osc) api.current.volume.setData([]);                          // the oscillator takes the bottom band instead of volume
    candles.priceScale().applyOptions({ scaleMargins: hasVol || osc ? { top: 0.06, bottom: 0.24 } : { top: 0.06, bottom: 0.06 } });

    for (const sr of api.current.ovl ?? []) chart.removeSeries(sr);     // the strategy's own lines, its stop path and RSI band
    api.current.ovl = [];
    const upto = (vals) => { const d = []; for (let i = first; i <= at; i++) d.push(vals[i] == null ? { time: calendar[i] } : { time: calendar[i], value: vals[i] }); return d; };
    (overlay?.lines ?? []).forEach((ln, k) => {
      const sr = chart.addLineSeries({ color: OVERLAY_COLORS[k % OVERLAY_COLORS.length], lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, title: "" });
      sr.setData(upto(ln.values)); api.current.ovl.push(sr);
    });
    if (stopPath && stopPath.some((v) => v != null)) {
      const sr = chart.addLineSeries({ color: colors.loss, lineWidth: 2, lineStyle: LineStyle.Dotted, lineType: LineType.WithSteps, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      sr.setData(upto(stopPath)); api.current.ovl.push(sr);
    }
    if (osc) {
      const sr = chart.addLineSeries({ color: OVERLAY_COLORS[2], lineWidth: 1, priceScaleId: "osc", lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      chart.priceScale("osc").applyOptions({ scaleMargins: { top: 0.8, bottom: 0.02 } });
      sr.setData(upto(osc.values)); api.current.ovl.push(sr);
      for (const lv of osc.levels ?? []) sr.createPriceLine({ price: lv, color: colors.muted, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: String(lv) });
    }

    const markers = [];
    for (const t of trades ?? []) {
      const sh = t.side === "short";
      const entryPx = t.entry != null ? t.entry.toFixed(2) : "-";
      markers.push({ time: calendar[t.i0], position: sh ? "aboveBar" : "belowBar", shape: sh ? "arrowDown" : "arrowUp", color: colors.accent, text: `${sh ? "SHORT" : "BUY"} ${qtyFmt(t.qty)} @ ${entryPx}` });
      for (const a of t.adds ?? []) markers.push({ time: calendar[a.i], position: "belowBar", shape: "arrowUp", color: colors.accent, text: `ADD ${qtyFmt(a.q)} @ ${a.px != null ? a.px.toFixed(2) : "-"}` });
      if (t.i1 != null) markers.push({ time: calendar[t.i1], position: sh ? "belowBar" : "aboveBar", shape: sh ? "arrowUp" : "arrowDown", color: t.pnl >= 0 ? colors.gain : colors.loss, text: `${sh ? "COVER" : "SELL"} ${qtyFmt(t.soldQty)} ${signed(t.pnl)}` });
    }
    for (const m of overlay?.markers ?? []) if (m.i >= first && m.i <= at) markers.push({ time: calendar[m.i], position: "aboveBar", shape: "circle", color: colors.muted, text: m.text });
    candles.setMarkers(markers.filter((m) => m.time >= calendar[first]).sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));

    for (const s of api.current.lines) chart.removeSeries(s);
    api.current.lines = (trades ?? []).map((t) => {
      const done = t.i1 != null, end = done ? t.i1 : at;
      if (end <= t.i0) return null;
      const color = done ? (t.pnl >= 0 ? colors.gain : colors.loss) : colors.accent;
      const s = chart.addLineSeries({ color, lineWidth: 2, lineStyle: done ? LineStyle.Solid : LineStyle.Dashed, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData([{ time: calendar[t.i0], value: t.entry }, { time: calendar[end], value: done ? t.exit : (bars && bars[at] ? bars[at][3] : t.entry) }]);
      return s;
    }).filter(Boolean);

    for (const pl of api.current.focusLines ?? []) candles.removePriceLine(pl);
    api.current.focusLines = [];
    if (api.current.priceLine) { candles.removePriceLine(api.current.priceLine); api.current.priceLine = null; }
    const ft = focus ? (trades ?? []).find((t) => t.i0 <= focus.at && (t.i1 ?? at) >= focus.at) : null;
    if (ft) {                                                            // the clicked trade: its buy, stop and sell as labelled lines
      const line = (price, color, title) => price != null && api.current.focusLines.push(candles.createPriceLine({ price, color, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title }));
      line(ft.entry, colors.accent, "bought");
      line(ft.stop, colors.loss, "first stop");
      if (ft.exit != null) line(ft.exit, ft.pnl >= 0 ? colors.gain : colors.loss, "sold");
    } else {
      const open = (trades ?? []).find((t) => t.i1 == null);
      if (open) api.current.priceLine = candles.createPriceLine({ price: open.entry, color: colors.accent, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: "bought" });
    }

    const key = `${symbol}|${first}|${last}|${fitKey}|${colors.bg}|${focus?.token ?? ""}`;
    if (lastRange.current !== key) {                         // fix the axis to the whole range; only when the range changes, so the user's zoom survives playback
      lastRange.current = key;
      const span = calendar.slice(first, last + 1).length;
      if (ft) {
        const a = Math.max(0, ft.i0 - 15), b = Math.min(calendar.length - 1, (ft.i1 ?? at) + 15);
        chart.timeScale().setVisibleRange({ from: calendar[a], to: calendar[b] });
      } else {
        chart.timeScale().setVisibleLogicalRange({ from: -1, to: span + 1 });
      }
    }
  }, [colors, symbol, bars, calendar, first, last, at, trades, fitKey, showVolume, focus, overlay, stopPath]);

  const dayIndex = new Map(calendar.map((d, i) => [String(d), i]));
  const bar = hover ?? (bars && bars[at] ? { open: bars[at][0], high: bars[at][1], low: bars[at][2], close: bars[at][3], volume: bars[at][4], time: calendar[at] } : null);
  return (
    <div className="tv">
      {bar && (
        <div className="tv-legend">
          <b>{symbol}</b><span className="tf" title={tf === "1M" ? "one candle per month" : tf === "4H" ? "one candle per 4 hours (UTC)" : "one candle per day"}>{tf}</span><span>{typeof bar.time === "number" ? new Date(bar.time * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : String(bar.time)}</span>
          <span>O <i>{bar.open.toFixed(2)}</i></span><span>H <i>{bar.high.toFixed(2)}</i></span><span>L <i>{bar.low.toFixed(2)}</i></span>
          <span>C <i className={bar.close >= bar.open ? "up" : "down"}>{bar.close.toFixed(2)}</i></span>
          {showVolume && bar.volume != null && <span>Vol <i>{Math.round(bar.volume).toLocaleString("en-IN")}</i></span>}
          {(() => { const i = dayIndex.get(String(bar.time)); if (i == null) return null;
            return <>{(overlay?.lines ?? []).map((ln, k) => ln.values[i] != null && <span key={ln.name} style={{ color: OVERLAY_COLORS[k % OVERLAY_COLORS.length] }}>{ln.name} <i>{ln.values[i].toFixed(2)}</i></span>)}
              {stopPath?.[i] != null && <span className="down">stop <i>{stopPath[i].toFixed(2)}</i></span>}
              {overlay?.osc?.values[i] != null && <span>{overlay.osc.name} <i>{overlay.osc.values[i].toFixed(1)}</i></span>}</>; })()}
        </div>
      )}
      <div ref={box} className="tv-box tv-price" />
    </div>
  );
}
