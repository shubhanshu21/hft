// node --test tests/  -- Ctrl+K search and the engine-schedule countdown (2026-10-29 layout redesign)
import test from "node:test";
import assert from "node:assert/strict";
import { paletteItems, filterItems, minutesUntil, untilText } from "../src/lib/palette.js";

const items = paletteItems([{ id: "RESULTS_GAP", label: "Results-day gap-up", strategy: "results_gap" }],
  [{ id: "abc", strategy: "trend_carry", market: "commodity", params: { start: "2026-01-01", end: "2026-10-25" } }]);

test("every page, account and saved run is searchable", () => {
  assert.equal(items.length, 6);
  assert.deepEqual(filterItems(items, "gap").map((i) => i.href), ["#/paper/RESULTS_GAP"]);
  assert.deepEqual(filterItems(items, "trend 2026").map((i) => i.href), ["#/results/abc"]);
  assert.equal(filterItems(items, "").length, 6);
  assert.equal(filterItems(items, "nothing-like-this").length, 0);
});

test("countdown to the next engine run, in IST", () => {
  const now = new Date("2026-10-29T07:30:00Z");                 // Thu 13:00 IST
  assert.equal(minutesUntil("Running · next at 13:05 IST", now), 5);
  assert.equal(minutesUntil("next at Thu 23:40 IST", now), 640);
  assert.equal(minutesUntil("next at Fri 09:10 IST", now), 1210);
  assert.equal(minutesUntil("next at Thu 09:10 IST", now), 7 * 1440 - 230);   // already past today -> next week
  assert.equal(minutesUntil("no schedule", now), null);
  assert.equal(untilText(5), "in 5 min");
  assert.equal(untilText(640), "in 10h 40m");
});

import { readCollapsed, saveCollapsed } from "../src/lib/palette.js";

test("sidebar collapse is remembered, and a broken storage never crashes", () => {
  const mem = { v: {}, getItem(k) { return this.v[k] ?? null; }, setItem(k, x) { this.v[k] = x; } };
  assert.equal(readCollapsed(mem), false);
  saveCollapsed(mem, true); assert.equal(readCollapsed(mem), true);
  saveCollapsed(mem, false); assert.equal(readCollapsed(mem), false);
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(readCollapsed(broken), false);
  saveCollapsed(broken, true);
  assert.equal(readCollapsed(null), false);
});
