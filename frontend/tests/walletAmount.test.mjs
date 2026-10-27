// node --test tests/  -- the account card must match the big number on the paper page (2026-10-27)
import test from "node:test";
import assert from "node:assert/strict";
import { walletAmount } from "../src/lib/format.js";

test("selected card shows the live value (booked + open trades)", () => {
  assert.deepEqual(walletAmount({ booked: 250000, open: 1 }, true, 250170), { amount: 250170, note: "1 open" });
});

test("other cards show booked money and say so while trades are open", () => {
  assert.deepEqual(walletAmount({ booked: 100000, open: 2 }, false, 250170), { amount: 100000, note: "booked · 2 open" });
  assert.deepEqual(walletAmount({ booked: 100000, open: 0 }, false, 250170), { amount: 100000, note: "0 open" });
});

test("selected card falls back to booked before the value is known", () => {
  assert.equal(walletAmount({ booked: 250000, open: 1 }, true, null).amount, 250000);
});
