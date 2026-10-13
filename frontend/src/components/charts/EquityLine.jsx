import { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";
import { baseOptions } from "./chartOptions.js";
import { useTheme, withAlpha } from "../../hooks/useTheme.js";

/** Account value as a TradingView-style area chart: green above the starting money, red below, a dot on every closed trade. Axis spans the whole run. */
export default function EquityLine({ result, upto, start }) {
  const colors = useTheme();
  const box = useRef(null);
  const api = useRef({});
  const calendar = result.days_all ?? result.days;

  useEffect(() => {
    const chart = createChart(box.current, baseOptions(colors, 240));
    const series = chart.addBaselineSeries({
      baseValue: { type: "price", price: start },
      topLineColor: colors.gain, topFillColor1: withAlpha(colors.gain, 0.3), topFillColor2: withAlpha(colors.gain, 0.02),
      bottomLineColor: colors.loss, bottomFillColor1: withAlpha(colors.loss, 0.02), bottomFillColor2: withAlpha(colors.loss, 0.3),
      lineWidth: 2, priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` },
    });
    const bench = chart.addLineSeries({ color: colors.muted, lineWidth: 1, lineStyle: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
                                        title: "buy & hold", priceFormat: { type: "custom", formatter: (v) => `\u20B9${Math.round(v).toLocaleString("en-IN")}` } });
    api.current = { chart, series, bench, fitted: false };
    return () => { chart.remove(); api.current = {}; };
  }, [colors, start]);

  useEffect(() => {
    const { chart, series } = api.current;
    if (!chart) return;
    series.setData(result.equity.slice(0, upto + 1).map((v, i) => ({ time: calendar[i], value: v })));
    api.current.bench.setData((result.benchmark ?? []).slice(0, upto + 1).map((v, i) => ({ time: calendar[i], value: v })));
    const marks = [];
    for (let k = 0; k <= upto; k++) for (const e of result.events[k]) if (e.t === "sell") marks.push({ time: calendar[k], position: "inBar", shape: "circle", color: e.pnl >= 0 ? colors.gain : colors.loss });
    series.setMarkers(marks.filter((m, i, a) => i === 0 || m.time !== a[i - 1].time || m.color !== a[i - 1].color));
    if (!api.current.fitted) { api.current.fitted = true; chart.timeScale().setVisibleLogicalRange({ from: -1, to: calendar.length + 1 }); }
  }, [colors, result, upto, calendar]);

  return <div ref={box} className="tv-box" />;
}
