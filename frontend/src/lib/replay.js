/** Helpers over a replay result: { days, equity, events, pos, bars, metrics }. */

/** Totals of everything that closed up to (and including) day index `upto`. */
export function tally(result, upto) {
  let n = 0, wins = 0, profit = 0, fees = 0, slippage = 0;
  for (let k = 0; k <= upto; k++) for (const e of result.events[k]) {
    if (e.t !== "sell") continue;
    n++; wins += e.pnl > 0; profit += e.pnl; fees += e.fees; slippage += e.slip ?? 0;
  }
  return { n, wins, profit, fees, slippage };
}

/** Symbols that have candles in this result. */
export const symbolsWithBars = (result) => Object.keys(result.bars ?? {});

/** The symbol the price chart should follow on day `at`: an open holding, else the latest trade, else the first symbol. */
export function followedSymbol(result, at) {
  const held = result.pos[at]?.[0]?.sym;
  if (held) return held;
  for (let k = at; k >= 0; k--) { const e = result.events[k][0]; if (e) return e.sym; }
  return symbolsWithBars(result)[0] ?? null;
}

/** Buy / sell markers of one symbol up to day `upto`: [{ i, t, price, pnl }]. */
export function markersFor(result, sym, upto) {
  const out = [];
  for (let k = 0; k <= upto; k++) for (const e of result.events[k]) if (e.sym === sym) out.push({ i: k, t: e.t, price: e.px, pnl: e.pnl });
  return out;
}

/**
 * Round trips per symbol up to day `upto`: [{ sym, i0, i1, entry, exit, pnl, why }] (i1 / exit / pnl are null while still open).
 * One position per symbol at a time, so each sell closes that symbol's latest buy.
 */
export function tradesBySymbol(result, upto) {
  const open = {}, bySym = {};
  for (let k = 0; k <= upto; k++) for (const e of result.events[k]) {
    if (e.t === "buy" && open[e.sym]) open[e.sym].adds.push({ i: k, px: e.px, q: e.q });          // pyramiding: a second buy into the same trade
    else if (e.t === "buy") open[e.sym] = { sym: e.sym, side: e.side ?? "long", i0: k, i1: null, entry: e.px, qty: e.q, soldQty: null, stop: e.stop ?? null, exit: null, pnl: null, why: null, adds: [] };
    else if (open[e.sym]) {
      const t = { ...open[e.sym], i1: k, exit: e.px, soldQty: e.q, pnl: e.pnl, why: e.why, fees: e.fees, days: e.days };
      (bySym[e.sym] ??= []).push(t);
      delete open[e.sym];
    }
  }
  for (const t of Object.values(open)) (bySym[t.sym] ??= []).push(t);
  return bySym;
}

/** A quantity as people read it: shares for stocks; for MCX futures results (result.lots) "2 lots · 20 barrels" (short: "2 lots"). */
export function qtyText(result, sym, q, short = false) {
  const l = result?.lots?.[sym];
  if (q == null) return "-";
  if (!l) return q;
  const lots = Math.round(q / l.q_per_lot);
  const amount = +(lots * l.per_lot).toFixed(2);
  const unit = l.unit === "tonne" && amount !== 1 ? "tonnes" : l.unit;
  const head = `${lots} lot${lots === 1 ? "" : "s"}`;
  return short ? head : `${head} · ${amount.toLocaleString("en-IN")} ${unit}`;
}

/** Column heading for quantities: "Lots" for futures results, else "Shares". */
export const qtyHeader = (result) => (result?.lots ? "Lots" : "Shares");
