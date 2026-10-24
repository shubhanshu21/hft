export const inr = (v) => (v < 0 ? "-" : "") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
export const signed = (v) => (v >= 0 ? "+" : "-") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
export const tone = (v) => (v >= 0 ? "up" : "down");
const parse = (s) => new Date(s + "T00:00:00");
export const longDate = (s) => parse(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
export const shortDate = (s) => parse(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
export const monthName = (s) => parse(s).toLocaleDateString("en-GB", { month: "short" });
export const pct = (v, d = 1) => `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;
/** A quantity people can read: whole numbers from 1,000 up, 2 decimals from 1, otherwise up to 4 significant digits (0.0123). */
export const qty = (q) => {
  if (q == null || Number.isNaN(+q)) return "-";
  const a = Math.abs(+q);
  const d = a >= 1000 ? 0 : a >= 1 ? 2 : 4;
  return (+q).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: 0 });
};
/** A price: 4 decimals under 10 (DOGE 0.0908 must not read 0.09), 2 above. */
export const price = (p) => (p == null || Number.isNaN(+p) ? "-" : (+p).toFixed(Math.abs(+p) < 10 ? 4 : 2));
/** The engine's exit codes in plain words. */
const WHY = { exit_signal: "price bounced back (exit signal)", stop: "stop-loss hit", target: "target hit", time: "held 24 hours (time limit)",
  monthly_rebalance: "monthly rebalance", "Monthly rebalance": "monthly rebalance" };
export const whyText = (w) => (w == null ? "" : WHY[w] ?? String(w).replace(/_/g, " "));
