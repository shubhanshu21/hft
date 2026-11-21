// node --test tests/  -- the results headline splits the change since the start into sold trades, open trades and tax (2026-11-21:
// a crypto run after business tax said "-Rs67 lakh loss on 6 open trades" -- the 67 lakh was the income tax, and nothing was open)
import test from "node:test";
import assert from "node:assert/strict";
import { splitChange } from "../src/lib/replay.js";

const sell = (pnl) => ({ t: "sell", pnl, fees: 0 });

test("no tax in the result: whatever is not sold profit is the open trades", () => {
  const r = { equity: [100, 130], events: [[], [sell(20)]], pos: [[], [{ sym: "A", q: 1, e: 10 }]], bars: { A: [null, [10, 21, 9, 20]] }, metrics: { start_capital: 100 } };
  assert.deepEqual(splitChange(r, 1), { sold: 20, open: 10, tax: 0, nOpen: 1 });
});

test("after tax: open trades are valued at the day's close, the rest is tax", () => {
  const r = { equity: [100, 150], events: [[], [sell(80)]], pos: [[], [{ sym: "A", q: 2, e: 10 }]], bars: { A: [null, [10, 12, 9, 11]] },
              metrics: { start_capital: 100, tax_mode: "business" } };
  assert.deepEqual(splitChange(r, 1), { sold: 80, open: 2, tax: -32, nOpen: 1 });
});

test("after tax with nothing open: all of the gap is tax", () => {
  const r = { equity: [100, 140], events: [[], [sell(60)]], pos: [[], []], bars: {}, metrics: { start_capital: 100, tax_mode: "strict" } };
  assert.deepEqual(splitChange(r, 1), { sold: 60, open: 0, tax: -20, nOpen: 0 });
});

test("tax mode none behaves like no tax", () => {
  const r = { equity: [100, 105], events: [[], []], pos: [[], [{ sym: "A", q: 1, e: 10 }]], bars: { A: [null, [10, 15, 9, 15]] }, metrics: { start_capital: 100, tax_mode: "none" } };
  assert.deepEqual(splitChange(r, 1), { sold: 0, open: 5, tax: 0, nOpen: 1 });
});
