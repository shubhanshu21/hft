import { useEffect, useRef, useState } from "react";
import { createChart, LineStyle } from "lightweight-charts";
import { baseOptions } from "./chartOptions.js";
import { useTheme } from "../../hooks/useTheme.js";
import { signed } from "../../lib/format.js";

/**
 * TradingView-style candlestick chart (lightweight-charts) of one symbol with every trade the strategy took in it:
 * an arrow at each buy and sell, and a line from entry to exit (green = profit, red = loss, dashed = still open).
 * Drag to pan, scroll to zoom, hover for the crosshair. The time axis spans the whole run, so playback fills it from the left.
 * bars[i] = [open, high, low, close] | null; calendar[i] = "YYYY-MM-DD"; trades = [{ i0, i1, entry, exit, pnl, q }].
 */
export default function PriceChart({ symbol, bars, calendar, first, last, at, trades, fitKey }) {
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
    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(candles);
      setHover(d ? { ...d, time: p.time } : null);
    });
    api.current = { chart, candles, lines: [], priceLine: null };
    lastRange.current = "";
    return () => { chart.remove(); api.current = {}; };
  }, [colors, symbol]);

  useEffect(() => {
    const { chart, candles } = api.current;
    if (!chart) return;
    const data = [];
    for (let i = first; i <= at; i++) if (bars[i]) data.push({ time: calendar[i], open: bars[i][0], high: bars[i][1], low: bars[i][2], close: bars[i][3] });
    candles.setData(data);

    const markers = [];
    for (const t of trades) {
      markers.push({ time: calendar[t.i0], position: "belowBar", shape: "arrowUp", color: colors.accent, text: `BUY ${t.entry.toFixed(2)}` });
      if (t.i1 != null) markers.push({ time: calendar[t.i1], position: "aboveBar", shape: "arrowDown", color: t.pnl >= 0 ? colors.gain : colors.loss, text: `SELL ${signed(t.pnl)}` });
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

    if (api.current.priceLine) { candles.removePriceLine(api.current.priceLine); api.current.priceLine = null; }
    const open = trades.find((t) => t.i1 == null);
    if (open) api.current.priceLine = candles.createPriceLine({ price: open.entry, color: colors.accent, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: "bought" });

    const key = `${symbol}|${first}|${last}|${fitKey}|${colors.bg}`;
    if (lastRange.current !== key) {                         // fix the axis to the whole range; only when the range changes, so the user's zoom survives playback
      lastRange.current = key;
      const span = calendar.slice(first, last + 1).length;
      chart.timeScale().setVisibleLogicalRange({ from: -1, to: span + 1 });
    }
  }, [colors, symbol, bars, calendar, first, last, at, trades, fitKey]);

  const bar = hover ?? (bars[at] ? { open: bars[at][0], high: bars[at][1], low: bars[at][2], close: bars[at][3], time: calendar[at] } : null);
  return (
    <div className="tv">
      {bar && (
        <div className="tv-legend">
          <b>{symbol}</b><span>{String(bar.time)}</span>
          <span>O <i>{bar.open.toFixed(2)}</i></span><span>H <i>{bar.high.toFixed(2)}</i></span><span>L <i>{bar.low.toFixed(2)}</i></span>
          <span>C <i className={bar.close >= bar.open ? "up" : "down"}>{bar.close.toFixed(2)}</i></span>
        </div>
      )}
      <div ref={box} className="tv-box" />
    </div>
  );
}
