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
