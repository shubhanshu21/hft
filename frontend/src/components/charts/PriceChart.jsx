import { useEffect, useRef, useState } from "react";
import { createChart, LineStyle } from "lightweight-charts";
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
export default function PriceChart({ qtyFmt = (q) => q ?? "", symbol, bars, calendar, first, last, at, trades, fitKey, showVolume = true, focus = null }) {
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
    if (!chart) return;
    const data = [];
    for (let i = first; i <= at; i++) if (bars[i]) data.push({ time: calendar[i], open: bars[i][0], high: bars[i][1], low: bars[i][2], close: bars[i][3] });
    candles.setData(data);
    const hasVol = showVolume && bars.some((b) => b && b[4] != null);
    const vols = [];
    if (hasVol) for (let i = first; i <= at; i++) if (bars[i] && bars[i][4] != null) vols.push({ time: calendar[i], value: bars[i][4], color: withAlpha(bars[i][3] >= bars[i][0] ? colors.gain : colors.loss, 0.45) });
    api.current.volume.setData(vols);
    candles.priceScale().applyOptions({ scaleMargins: hasVol ? { top: 0.06, bottom: 0.24 } : { top: 0.06, bottom: 0.06 } });

    const markers = [];
    for (const t of trades) {
      markers.push({ time: calendar[t.i0], position: "belowBar", shape: "arrowUp", color: colors.accent, text: `BUY ${qtyFmt(t.qty)} @ ${t.entry.toFixed(2)}` });
      for (const a of t.adds ?? []) markers.push({ time: calendar[a.i], position: "belowBar", shape: "arrowUp", color: colors.accent, text: `ADD ${qtyFmt(a.q)} @ ${a.px.toFixed(2)}` });
      if (t.i1 != null) markers.push({ time: calendar[t.i1], position: "aboveBar", shape: "arrowDown", color: t.pnl >= 0 ? colors.gain : colors.loss, text: `SELL ${qtyFmt(t.soldQty)} ${signed(t.pnl)}` });
    }
    candles.setMarkers(markers.filter((m) => m.time >= calendar[first]).sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));

    for (const s of api.current.lines) chart.removeSeries(s);
    api.current.lines = trades.map((t) => {
      const done = t.i1 != null, end = done ? t.i1 : at;
      if (end <= t.i0) return null;
      const color = done ? (t.pnl >= 0 ? colors.gain : colors.loss) : colors.accent;
      const s = chart.addLineSeries({ color, lineWidth: 2, lineStyle: done ? LineStyle.Solid : LineStyle.Dashed, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData([{ time: calendar[t.i0], value: t.entry }, { time: calendar[end], value: done ? t.exit : bars[at]?.[3] ?? t.entry }]);
      return s;
    }).filter(Boolean);

    for (const pl of api.current.focusLines ?? []) candles.removePriceLine(pl);
    api.current.focusLines = [];
    if (api.current.priceLine) { candles.removePriceLine(api.current.priceLine); api.current.priceLine = null; }
    const ft = focus ? trades.find((t) => t.i0 <= focus.at && (t.i1 ?? at) >= focus.at) : null;
    if (ft) {                                                            // the clicked trade: its buy, stop and sell as labelled lines
      const line = (price, color, title) => price != null && api.current.focusLines.push(candles.createPriceLine({ price, color, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title }));
      line(ft.entry, colors.accent, "bought");
      line(ft.stop, colors.loss, "first stop");
      if (ft.exit != null) line(ft.exit, ft.pnl >= 0 ? colors.gain : colors.loss, "sold");
    } else {
      const open = trades.find((t) => t.i1 == null);
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
  }, [colors, symbol, bars, calendar, first, last, at, trades, fitKey, showVolume, focus]);

  const bar = hover ?? (bars[at] ? { open: bars[at][0], high: bars[at][1], low: bars[at][2], close: bars[at][3], volume: bars[at][4], time: calendar[at] } : null);
  return (
    <div className="tv">
      {bar && (
        <div className="tv-legend">
          <b>{symbol}</b><span>{String(bar.time)}</span>
          <span>O <i>{bar.open.toFixed(2)}</i></span><span>H <i>{bar.high.toFixed(2)}</i></span><span>L <i>{bar.low.toFixed(2)}</i></span>
          <span>C <i className={bar.close >= bar.open ? "up" : "down"}>{bar.close.toFixed(2)}</i></span>
          {showVolume && bar.volume != null && <span>Vol <i>{Math.round(bar.volume).toLocaleString("en-IN")}</i></span>}
        </div>
      )}
      <div ref={box} className="tv-box" />
    </div>
  );
}
