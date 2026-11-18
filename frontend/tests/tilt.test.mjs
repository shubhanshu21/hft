// node --test tests/  -- the pointer tilt maths (2026-11-18)
import test from "node:test";
import assert from "node:assert/strict";
import { tiltAngles } from "../src/lib/tilt.js";

test("tilt leans toward the pointer, flat in the middle, capped at max", () => {
  const r = { left: 0, top: 0, width: 200, height: 100 };
  assert.deepEqual(tiltAngles(r, 100, 50), { rx: 0, ry: 0 });
  assert.deepEqual(tiltAngles(r, 200, 0, 3), { rx: 3, ry: 3 });        // top-right corner: top tips back, right side away
  assert.deepEqual(tiltAngles(r, 0, 100, 3), { rx: -3, ry: -3 });
});
