export const inr = (v) => (v < 0 ? "-" : "") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
export const signed = (v) => (v >= 0 ? "+" : "-") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
export const tone = (v) => (v >= 0 ? "up" : "down");
const parse = (s) => new Date(s + "T00:00:00");
export const longDate = (s) => parse(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
export const shortDate = (s) => parse(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
export const monthName = (s) => parse(s).toLocaleDateString("en-GB", { month: "short" });
export const pct = (v, d = 1) => `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;
