// node --test tests/  -- which saved result the Results page opens on, and how the list is grouped (2026-10-29)
import test from "node:test";
import assert from "node:assert/strict";
import { defaultRunKey, groupRuns } from "../src/lib/runs.js";

const runs = [
  { id: "a1", strategy: "pairs_cash", live: false },
  { id: "b2", strategy: "results_gap", live: true },
  { id: "c3", strategy: "trend_carry", live: true },
];
const samples = [{ file: "pairs_cash_2026.json", title: "Pairs trading" }];

test("an explicit run in the URL always wins", () => {
  assert.equal(defaultRunKey("zz", runs, samples), "zz");
});

test("otherwise the newest run of a live strategy, not an old example", () => {
  assert.equal(defaultRunKey(undefined, runs, samples), "b2");
});

test("falls back to any saved run, then an example, then nothing", () => {
  assert.equal(defaultRunKey(undefined, [{ id: "x", live: false }], samples), "x");
  assert.equal(defaultRunKey(undefined, [], samples), "sample:pairs_cash_2026.json");
  assert.equal(defaultRunKey(undefined, [], []), "");
});

test("groups: live strategies, other saved runs, old examples (empty groups dropped)", () => {
  const g = groupRuns(runs, samples);
  assert.deepEqual(g.map((x) => [x.label, x.items.length]), [["Live strategies", 2], ["Other saved runs", 1], ["Old examples", 1]]);
  assert.deepEqual(groupRuns([], []), []);
});
