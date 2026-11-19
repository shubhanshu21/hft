/** Helpers for the Today page. */
export const pctOf = (value, start) => (start ? Math.round((value / start - 1) * 1000) / 10 : 0);

export const statusTone = (engine) => (engine?.ok === true ? "good" : engine?.ok === false ? "bad" : "unknown");

/** Account colours shared by the ticker, donut, tables and wallet cards (Look B, 2026-10-29). */
export const ACCOUNT_COLOR = { CRYPTO_DIP_REVERSAL: "#7c3aed", RESULTS_GAP: "#059669", EQUITY_BREAKOUT: "#2563eb" };
export const ACCOUNT_SHORT = { CRYPTO_DIP_REVERSAL: "Crypto", RESULTS_GAP: "Equity", EQUITY_BREAKOUT: "Breakout" };

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

/** Desk "Portfolio value" line (2026-11-18): every account's booked value added up BY DATE (it used to add them by position, so an
 *  account that started later was lined up with the others' first days). Before its first point an account counts its starting money;
 *  the last point is the live total. equities[i] = account i's [{date, current_capital}]. */
export function sumByDate(accounts, equities, liveTotal) {
  const dates = [...new Set(equities.flatMap((e) => (e ?? []).map((x) => x.date)))].sort();
  const vals = dates.map((d) => accounts.reduce((s, a, i) => {
    const before = (equities[i] ?? []).filter((x) => x.date <= d);
    return s + (before.length ? before[before.length - 1].current_capital : a.start);
  }, 0));
  return liveTotal == null ? vals : [...vals, liveTotal];
}

/** When an engine looks next: its `next` ISO time when the API sends one, else the "next at ..." text. {mins, label}. */
export function nextRun(engine, now = new Date(), fromText = () => null) {
  if (engine?.next) {
    const t = new Date(engine.next);
    const label = t.toLocaleString("en-IN", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
    return { mins: Math.max(0, Math.round((t - now) / 60000)), label };
  }
  const m = String(engine?.text || "").match(/next at ([^·]+)/);
  return { mins: fromText(engine?.text, now), label: m ? m[1].replace(" IST", "").trim() : "-" };
}

/** 3D Desk towers: accounts in a row, centred, `gap` apart; height 0.6..4 scaled to the biggest value (a broken account is left out). */
export function towerLayout(accounts, gap = 2.1) {
  const ok = (accounts ?? []).filter((a) => !a.error && a.value > 0);
  const max = Math.max(1, ...ok.map((a) => a.value));
  return ok.map((a, i) => ({ id: a.id, x: (i - (ok.length - 1) / 2) * gap, height: 0.6 + 3.4 * (a.value / max) }));
}
