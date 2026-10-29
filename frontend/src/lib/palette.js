/** Ctrl+K quick search: every page, account and saved backtest as one list, filtered by the words typed (all must match). */
export function paletteItems(accounts = [], runs = []) {
  return [
    { label: "Desk", hint: "All accounts at a glance", href: "#/today", group: "Pages" },
    { label: "Accounts", hint: "Paper trading accounts", href: "#/paper", group: "Pages" },
    { label: "Research", hint: "Backtest results", href: "#/results", group: "Pages" },
    { label: "Backtest", hint: "Run a new backtest", href: "#/new", group: "Pages" },
    ...accounts.map((a) => ({ label: a.label, hint: a.strategy, href: `#/paper/${a.id}`, group: "Accounts" })),
    ...runs.map((r) => ({ label: r.label || `${r.strategy} on ${r.market}`, hint: `${r.params?.start ?? ""} to ${r.params?.end ?? ""}`, href: `#/results/${r.id}`, group: "Backtests" })),
  ];
}

export function filterItems(items, query, limit = 12) {
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const hit = words.length ? items.filter((it) => { const text = `${it.label} ${it.hint} ${it.group}`.toLowerCase(); return words.every((w) => text.includes(w)); }) : items;
  return hit.slice(0, limit);
}

/** "next at Thu 23:40 IST" -> minutes from `now` (IST), or null. A bare "next at 13:05" means today (or tomorrow if already past). */
export function minutesUntil(text, now = new Date()) {
  const m = String(text || "").match(/next at (?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?(\d\d):(\d\d)/);
  if (!m) return null;
  const ist = new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60000);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  let add = m[1] ? (days.indexOf(m[1]) - ist.getDay() + 7) % 7 : 0;
  let mins = add * 1440 + (+m[2] * 60 + +m[3]) - (ist.getHours() * 60 + ist.getMinutes());
  if (mins < 0) mins += m[1] ? 7 * 1440 : 1440;
  return mins;
}

export const untilText = (mins) => (mins == null ? "" : mins < 60 ? `in ${mins} min` : mins < 1440 ? `in ${Math.floor(mins / 60)}h ${mins % 60}m` : `in ${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`);

/** The sidebar's collapsed/expanded choice, remembered in this browser (private windows: always expanded). */
export const RAIL_KEY = "railCollapsed";
export function readCollapsed(storage) {
  try { return storage?.getItem(RAIL_KEY) === "1"; } catch { return false; }
}
export function saveCollapsed(storage, on) {
  try { storage?.setItem(RAIL_KEY, on ? "1" : "0"); } catch { /* private window */ }
}
