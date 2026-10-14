/** Numbers derived from a result for the analysis panels: monthly returns, drawdown, profit per stock, trades as CSV. */
import { tradesBySymbol } from "./replay.js";

/** { "2026": { 1: 2.3, 2: -1.1, ... , year: 11.2 } } -- % change of the account in each month (month-end vs previous month-end). */
export function monthlyReturns(days, equity, start) {
  const out = {};
  let prev = start, prevYearEnd = start, lastKey = null;
  days.forEach((d, i) => {
    const [y, m] = d.split("-");
    const key = `${y}-${m}`;
    const next = days[i + 1];
    if (!next || next.slice(0, 7) !== key) {
      (out[y] ??= {})[+m] = (equity[i] / prev - 1) * 100;
      prev = equity[i];
      if (!next || next.slice(0, 4) !== y) { out[y].year = (equity[i] / prevYearEnd - 1) * 100; prevYearEnd = equity[i]; }
    }
    lastKey = key;
  });
  return out;
}

/** % below the highest account value so far, per day (0 or negative). */
export function drawdown(equity) {
  let peak = -Infinity;
  return equity.map((v) => { peak = Math.max(peak, v); return (v / peak - 1) * 100; });
}

/** Closed and open trades per stock: count, winners, profit after fees, fees. */
export function profitByStock(result) {
  const by = tradesBySymbol(result, result.days.length - 1);
  return Object.entries(by).map(([sym, ts]) => {
    const done = ts.filter((t) => t.i1 != null);
    return { sym, trades: done.length, open: ts.length - done.length, wins: done.filter((t) => t.pnl > 0).length,
             profit: done.reduce((a, t) => a + t.pnl, 0), fees: done.reduce((a, t) => a + (t.fees ?? 0), 0) };
  }).sort((a, b) => b.profit - a.profit);
}

/** Every round trip as CSV text (opens a spreadsheet cleanly). */
export function tradesCsv(result) {
  const by = tradesBySymbol(result, result.days.length - 1);
  const rows = [["symbol", "bought", "shares_bought", "buy_price", "added", "sold", "shares_sold", "sell_price", "days_held", "profit_after_fees", "fees", "why_it_ended"]];
  for (const ts of Object.values(by)) for (const t of ts) {
    rows.push([t.sym, result.days[t.i0], t.qty ?? "", t.entry, (t.adds ?? []).map((a) => `${result.days[a.i]} ${a.q ?? ""}@${a.px}`).join(" "), t.i1 != null ? result.days[t.i1] : "open",
               t.soldQty ?? "", t.exit ?? "", t.days ?? "", t.pnl != null ? Math.round(t.pnl) : "", t.fees != null ? Math.round(t.fees) : "", t.why ?? ""]);
  }
  rows.sort((a, b) => (a[1] < b[1] ? -1 : 1));
  return rows.map((r) => r.map((v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v)).join(",")).join("\n");
}

export function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
