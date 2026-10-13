import { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";
import { baseOptions } from "./chartOptions.js";
import { useTheme, withAlpha } from "../../hooks/useTheme.js";
import { drawdown } from "../../lib/analysis.js";

/** How far below its best value the account was on each day (0% = at a new high). */
export default function DrawdownChart({ result, upto }) {
  const colors = useTheme();
  const box = useRef(null);
  const api = useRef({});
  const calendar = result.days_all ?? result.days;
  useEffect(() => {
    const chart = createChart(box.current, baseOptions(colors, 150));
    const s = chart.addAreaSeries({ lineColor: colors.loss, topColor: withAlpha(colors.loss, 0.05), bottomColor: withAlpha(colors.loss, 0.35), lineWidth: 1.5,
                                    priceFormat: { type: "custom", formatter: (v) => `${v.toFixed(1)}%` }, invertFilledArea: true });
    api.current = { chart, s, fitted: false };
    return () => { chart.remove(); api.current = {}; };
  }, [colors]);
  useEffect(() => {
    const { chart, s } = api.current;
    if (!chart) return;
    const dd = drawdown(result.equity.slice(0, upto + 1));
    s.setData(dd.map((v, i) => ({ time: calendar[i], value: v })));
    if (!api.current.fitted) { api.current.fitted = true; chart.timeScale().setVisibleLogicalRange({ from: -1, to: calendar.length + 1 }); }
  }, [result, upto, calendar]);
  return <div ref={box} className="tv-box" />;
}
