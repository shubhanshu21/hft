import { CrosshairMode } from "lightweight-charts";
import { withAlpha } from "../../hooks/useTheme.js";

// lightweight-charts has no bold option: draw the chart font (trade labels, prices on the axis) semi-bold by adding the weight whenever
// a canvas font using the chart family is set. Only fonts with this exact family are touched.
const CHART_FAMILY = '"IBM Plex Mono", ui-monospace, monospace';
if (typeof CanvasRenderingContext2D !== "undefined" && !CanvasRenderingContext2D.prototype.__boldChartFont) {
  const desc = Object.getOwnPropertyDescriptor(CanvasRenderingContext2D.prototype, "font");
  Object.defineProperty(CanvasRenderingContext2D.prototype, "font", {
    get() { return desc.get.call(this); },
    set(v) { desc.set.call(this, typeof v === "string" && v.includes(CHART_FAMILY) && !/\b(bold|[5-9]00)\b/.test(v) ? `600 ${v}` : v); },
    configurable: true,
  });
  CanvasRenderingContext2D.prototype.__boldChartFont = true;
}

/** Shared look for the TradingView-style charts, from the page's colour tokens. */
export const baseOptions = (c, height) => ({
  height,
  autoSize: true,
  layout: { background: { color: c.panel }, textColor: c.fg, fontFamily: CHART_FAMILY, fontSize: 12 },
  grid: { vertLines: { color: withAlpha(c.muted, 0.08) }, horzLines: { color: withAlpha(c.muted, 0.12) } },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: c.line },
  timeScale: { borderColor: c.line, rightOffset: 0, timeVisible: false },
  handleScale: { axisPressedMouseMove: true },
});
