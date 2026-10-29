/** Helpers for the Today page. */
export const pctOf = (value, start) => (start ? Math.round((value / start - 1) * 1000) / 10 : 0);

export const statusTone = (engine) => (engine?.ok === true ? "good" : engine?.ok === false ? "bad" : "unknown");

/** Account colours shared by the ticker, donut, tables and wallet cards (Look B, 2026-10-29). */
export const ACCOUNT_COLOR = { MCX_TREND_CARRY: "#d97706", CRYPTO_DIP_REVERSAL: "#7c3aed", RESULTS_GAP: "#059669" };
export const ACCOUNT_SHORT = { MCX_TREND_CARRY: "MCX", CRYPTO_DIP_REVERSAL: "Crypto", RESULTS_GAP: "Equity" };

/** Ticker tape: the total first, then each account and its open trades. */
export function tickerItems(ov) {
  if (!ov?.accounts) return [];
  const t = ov.totals;
  const out = [{ label: "Total", value: t.value, change: t.value - t.start, kind: "money" }];
  for (const a of ov.accounts) {
    if (a.error) continue;
    out.push({ label: ACCOUNT_SHORT[a.id] ?? a.label, value: a.value, change: pctOf(a.value, a.start), kind: "pct" });
    for (const p of a.open ?? []) out.push({ label: p.symbol, value: null, change: p.profit_now, kind: "money" });
  }
  return out;
}

/** Donut slices: each account's share of the total value, in order, as [start, length] fractions of the circle. */
export function donutSlices(accounts) {
  const ok = (accounts ?? []).filter((a) => !a.error && a.value > 0);
  const total = ok.reduce((s, a) => s + a.value, 0);
  let at = 0;
  return ok.map((a) => { const share = total ? a.value / total : 0; const s = { id: a.id, start: at, share }; at += share; return s; });
}
