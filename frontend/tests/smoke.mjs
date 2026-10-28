// Browser smoke test (2026-10-29): opens every page in real Chromium at desktop and phone size and fails on a console error, a
// missing key text, or sideways scrolling on a phone. Run against the deployed site (or a preview):
//   node tests/smoke.mjs [base-url]        e.g. node tests/smoke.mjs https://127.0.0.1/preview/
import { chromium } from "playwright-core";

const BASE = process.argv[2] || "https://127.0.0.1/";
const PAGES = [
  ["today", ["All 3 paper accounts", "Accounts", "Latest trades"]],
  ["paper/RESULTS_GAP", ["Paper trading", "Results-day gap-up", "Closed trades"]],
  ["paper/MCX_TREND_CARRY", ["MCX trend + carry", "Open trades"]],
  ["results", ["Result", "Where the money went"]],
  ["new", ["What to test", "Money, fees and slippage", "Run backtest"]],
];
const SIZES = [["desktop", 1440, 900], ["phone", 390, 844]];

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || "/snap/bin/chromium", args: ["--no-sandbox"] });
const failures = [];
for (const [size, width, height] of SIZES) {
  const ctx = await browser.newContext({ viewport: { width, height }, ignoreHTTPSErrors: true });
  for (const [route, texts] of PAGES) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(`${BASE}#/${route}`);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1500);
    const body = await page.textContent("body");
    for (const t of texts) if (!body.includes(t)) failures.push(`${size} #/${route}: missing "${t}"`);
    for (const e of errors) failures.push(`${size} #/${route}: console error: ${e.slice(0, 160)}`);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (size === "phone" && overflow > 1) failures.push(`${size} #/${route}: page scrolls sideways by ${overflow}px`);
    console.log(`${failures.length ? "…" : "ok"} ${size.padEnd(7)} #/${route}`);
    await page.close();
  }
  await ctx.close();
}
await browser.close();
if (failures.length) { console.error(`\n${failures.length} problem(s):\n- ${failures.join("\n- ")}`); process.exit(1); }
console.log(`\nAll ${PAGES.length * SIZES.length} page checks passed`);
