import { CrosshairMode } from "lightweight-charts";
import { withAlpha } from "../../hooks/useTheme.js";

/** Shared look for the TradingView-style charts, from the page's colour tokens. */
export const baseOptions = (c, height) => ({
  height,
  autoSize: true,
  layout: { background: { color: c.panel }, textColor: c.muted, fontFamily: '"IBM Plex Mono", ui-monospace, monospace', fontSize: 11 },
  grid: { vertLines: { color: withAlpha(c.muted, 0.08) }, horzLines: { color: withAlpha(c.muted, 0.12) } },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: c.line },
  timeScale: { borderColor: c.line, rightOffset: 0, timeVisible: false },
  handleScale: { axisPressedMouseMove: true },
});
