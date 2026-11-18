// node --test tests/  -- small helpers of the Today page (2026-10-29)
import test from "node:test";
import assert from "node:assert/strict";
import { pctOf, statusTone } from "../src/lib/overview.js";

test("percent change against the starting money", () => {
  assert.equal(pctOf(117728, 100000), 17.7);
  assert.equal(pctOf(95000, 100000), -5);
  assert.equal(pctOf(5, 0), 0);                  // no starting money: no division by zero
});

test("engine status tone: ok, problem, unknown", () => {
  assert.equal(statusTone({ ok: true }), "good");
  assert.equal(statusTone({ ok: false }), "bad");
  assert.equal(statusTone({}), "unknown");
  assert.equal(statusTone(undefined), "unknown");
});

import { tickerItems, donutSlices } from "../src/lib/overview.js";

const ov = { totals: { value: 475000, start: 450000 }, accounts: [
  { id: "MCX_TREND_CARRY", value: 251000, start: 250000, open: [{ symbol: "CRUDEOILM", profit_now: 1000 }] },
  { id: "CRYPTO_DIP_REVERSAL", value: 100000, start: 100000, open: [] },
  { id: "RESULTS_GAP", error: "unavailable" }] };

test("ticker: total, then each account and its open trades; broken accounts skipped", () => {
  const t = tickerItems(ov);
  assert.deepEqual(t.map((x) => x.label), ["Total", "MCX", "CRUDEOILM", "Crypto"]);
  assert.equal(t[0].change, 25000);
  assert.equal(t[1].change, 0.4);
  assert.deepEqual(tickerItems(null), []);
});

test("donut slices add up to the whole circle", () => {
  const s = donutSlices(ov.accounts);
  assert.equal(s.length, 2);
  assert.equal(s[1].start, s[0].share);
  assert.ok(Math.abs(s.reduce((a, x) => a + x.share, 0) - 1) < 1e-9);
  assert.deepEqual(donutSlices([]), []);
});

import { sumByDate, nextRun } from "../src/lib/overview.js";

test("portfolio line adds accounts by date; a later account counts its start money before its first day", () => {
  const accounts = [{ start: 100 }, { start: 50 }];
  const eq = [[{ date: "2026-10-24", current_capital: 100 }, { date: "2026-10-25", current_capital: 110 }],
              [{ date: "2026-10-25", current_capital: 49 }]];
  assert.deepEqual(sumByDate(accounts, eq, 170), [150, 159, 170]);
  assert.deepEqual(sumByDate(accounts, [[], []]), []);
});

test("next run from the engine's ISO time, falling back to its text", () => {
  const now = new Date("2026-11-18T12:00:00+05:30");
  const r = nextRun({ next: "2026-11-19T09:08:00+05:30", text: "Waiting for the first session · Thu 19 Nov 09:08 IST" }, now);
  assert.equal(r.mins, 21 * 60 + 8);
  assert.match(r.label, /Thu/); assert.match(r.label, /09:08/);
  assert.deepEqual(nextRun({ text: "Running · next at Thu 23:40 IST" }, now, () => 5), { mins: 5, label: "Thu 23:40" });
  assert.deepEqual(nextRun(undefined, now), { mins: null, label: "-" });
});

import { towerLayout } from "../src/lib/overview.js";

test("3D towers: centred row, tallest = biggest account, broken accounts left out", () => {
  const t = towerLayout([{ id: "A", value: 200 }, { id: "B", value: 100 }, { id: "C", error: "x" }]);
  assert.deepEqual(t.map((x) => x.id), ["A", "B"]);
  assert.equal(t[0].x, -1.05); assert.equal(t[1].x, 1.05);
  assert.equal(t[0].height, 4); assert.equal(t[1].height, 2.3);
  assert.deepEqual(towerLayout([]), []);
});
