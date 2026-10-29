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
