// Browser smoke test (2026-10-29): opens every page in real Chromium at desktop and phone size and fails on a console error, a
// missing key text, or sideways scrolling on a phone. Run against the deployed site (or a preview):
//   node tests/smoke.mjs [base-url]        e.g. node tests/smoke.mjs https://127.0.0.1/preview/
import { chromium } from "playwright-core";

const BASE = process.argv[2] || "https://127.0.0.1/";
const PAGES = [
  ["today", ["Total value", "Portfolio value", "Open positions", "Engine schedule", "Latest fills", "Paper accounts"]],
  ["paper/RESULTS_GAP", ["Results gap + earnings drift", "Account value", "Open positions", "Closed trades"]],
  ["paper/MCX_TREND_CARRY", ["MCX trend + carry", "Account value", "Engine"]],
  ["results", ["Saved results", "Where the money went"]],
  ["new", ["Backtest builder", "What to test", "Money, fees and slippage", "Run backtest"]],
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
// Ctrl+K quick search: type an account name, press Enter, land on that account
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  await page.goto(`${BASE}#/today`); await page.waitForLoadState("networkidle");
  await page.keyboard.press("Control+k");
  await page.waitForSelector(".pal input", { timeout: 5000 }).catch(() => failures.push("Ctrl+K did not open the search"));
  await page.keyboard.type("gap"); await page.keyboard.press("Enter"); await page.waitForTimeout(800);
  const hash = await page.evaluate(() => location.hash);
  if (hash !== "#/paper/RESULTS_GAP") failures.push(`Ctrl+K "gap" + Enter went to ${hash}, expected #/paper/RESULTS_GAP`);
  console.log(`${hash === "#/paper/RESULTS_GAP" ? "ok" : "…"} Ctrl+K search`);
  await ctx.close();
}
// Sidebar collapse: the fold button shrinks it, the choice survives a reload, "[" expands it again
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  await page.goto(`${BASE}#/new`); await page.waitForLoadState("networkidle");
  await page.click(".rail-fold");
  const width = async () => page.evaluate(() => document.querySelector(".rail").getBoundingClientRect().width);
  await page.waitForTimeout(400);
  const narrow = await width();
  await page.reload(); await page.waitForLoadState("networkidle"); await page.waitForTimeout(300);
  const after = await width();
  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
  await page.keyboard.press("["); await page.waitForTimeout(400);
  const wide = await width();
  const ok = narrow < 100 && after < 100 && wide > 200;
  if (!ok) failures.push(`sidebar collapse: ${narrow}px after the click, ${after}px after reload, ${wide}px after "["`);
  console.log(`${ok ? "ok" : "…"} sidebar collapse (${Math.round(narrow)} -> reload ${Math.round(after)} -> "[" ${Math.round(wide)}px)`);
  await ctx.close();
}
await browser.close();
if (failures.length) { console.error(`\n${failures.length} problem(s):\n- ${failures.join("\n- ")}`); process.exit(1); }
console.log(`\nAll ${PAGES.length * SIZES.length} page checks + the search and sidebar checks passed`);
