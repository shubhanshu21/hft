import Icon from "../ui/Icon.jsx";
import { useEffect, useMemo, useState } from "react";
import Panel from "../ui/Panel.jsx";

const today = () => new Date().toISOString().slice(0, 10);
const THIS_YEAR = new Date().getFullYear();
const FIRST_YEAR = 2022;                    // the archived daily candles start in September 2022
const YEARS = Array.from({ length: THIS_YEAR - FIRST_YEAR + 1 }, (_, i) => THIS_YEAR - i);
// Remembers the last market, strategy and dates used (this browser only; 2026-10-24) so a repeat test needs no re-typing.
const SAVED_KEY = "lastBacktest";
const loadSaved = () => { try { return JSON.parse(localStorage.getItem(SAVED_KEY) || "null"); } catch { return null; } };
const isoDaysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const yearRange = (y) => ({ start: `${y}-01-01`, end: y === THIS_YEAR ? today() : `${y}-12-31` });
const MARKET_LABEL = { commodity: "Commodity (MCX futures)", crypto: "Crypto (Delta Exchange India futures)", equity: "Equity (NSE intraday)" };

export default function BacktestForm({ strategies, onSubmit, busy }) {
  const markets = useMemo(() => [...new Set(strategies.map((s) => s.market))], [strategies]);
  const saved = useMemo(loadSaved, []);
  const [market, setMarket] = useState(saved && markets.includes(saved.market) ? saved.market : markets[0] ?? "equity");
  const options = strategies.filter((s) => s.market === market);
  const [name, setName] = useState(saved && options.some((s) => s.name === saved.strategy) ? saved.strategy : options[0]?.name ?? "");
  const strategy = options.find((s) => s.name === name) ?? options[0];
  useEffect(() => { if (strategy) setF((x) => ({ ...x, risk_pct: strategy.default_risk_pct ?? 2, exposure: strategy.exposure ?? 1, capital: strategy.default_capital ?? x.capital })); }, [strategy?.market, strategy?.name]);
  useEffect(() => {                                   // pre-tick the strategy's own list (what its paper trading uses) when it is narrower than the market
    const own = strategy?.default_symbols ?? [];
    const narrower = own.length > 0 && own.length < (strategy?.symbols?.length ?? 0);
    setAll(!narrower); setChosen(narrower ? own : []);
  }, [strategy?.market, strategy?.name]);   // start from the risk the strategy was tested with
  const safeRisk = Math.max(3, strategy?.default_risk_pct ?? 0);
  const monthly = !!strategy?.monthly_rank;          // commodity/trend_carry: ranks the chosen contracts together each month; exposure is editable
  const crypto = !!strategy?.crypto;
  const gap = strategy?.name === "results_gap";      // equity intraday: its own 414-stock list, size 1-5x
  const scalp = !!strategy?.scalp;                   // a crypto scalper (dip_reversal): coins chosen, no leverage box (risk-based sizing, leverage barely matters)
  const auto = !!strategy?.auto_universe;            // equity/breakout_pair: picks from the day's 200 most-traded NSE stocks itself, fixed 5 + 3 holdings
  const [all, setAll] = useState(true);
  const [chosen, setChosen] = useState([]);
  const [f, setF] = useState({ ...(saved?.start && saved?.end ? { start: saved.start, end: saved.end } : yearRange(THIS_YEAR)), capital: 100000, slippage_bp: 5, risk_pct: 2, max_positions: 3, leverage: 1, label: "", exposure: 1.5, tax: "business" });
  const [year, setYear] = useState(saved?.year ?? String(THIS_YEAR));       // "custom" once the dates are edited by hand

  useEffect(() => { const o = strategies.filter((s) => s.market === market); setName((n) => (o.some((s) => s.name === n) ? n : o[0]?.name ?? ""));
    setF((x) => ({ ...x, slippage_bp: market === "currency" ? 0 : 5 }));   // currency: costs include the real spread; futures margin is ~2-3%
  }, [market, strategies]);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const pickYear = (v) => { setYear(v); if (v !== "custom") setF({ ...f, ...yearRange(Number(v)) }); };
  const setDate = (k) => (e) => { setYear("custom"); setF({ ...f, [k]: e.target.value }); };
  const toggle = (s) => setChosen(chosen.includes(s) ? chosen.filter((x) => x !== s) : [...chosen, s]);

  const quick = (start, end, label) => { setYear(label); setF({ ...f, start, end }); };
  const submit = (e) => {
    e.preventDefault();
    try { localStorage.setItem(SAVED_KEY, JSON.stringify({ market, strategy: name, start: f.start, end: f.end, year })); } catch { /* private window */ }
    onSubmit({
      market, strategy: name, symbols: all ? strategy.symbols : chosen,
      start: f.start, end: f.end, capital: +f.capital, slippage_bp: market === "currency" ? 0 : +f.slippage_bp, risk_pct: +f.risk_pct, max_positions: +f.max_positions,
      exposure: +f.exposure, tax: f.tax,
      label: f.label,   // exits, trailing stops and adding to winners are the strategy's own (the extra overrides were removed from the form)
    });
  };

  return (
    <form onSubmit={submit} className="wrap steps">
      <Panel title="What to test">
        <div className="form-grid">
          <div className="field"><label htmlFor="market">Market</label>
            <select id="market" value={market} onChange={(e) => setMarket(e.target.value)}>
              {markets.map((m) => <option key={m} value={m}>{MARKET_LABEL[m] ?? m}</option>)}
            </select></div>
          <div className="field"><label htmlFor="strategy">Strategy</label>
            <select id="strategy" value={name} onChange={(e) => setName(e.target.value)}>
              {options.map((s) => <option key={s.name} value={s.name} disabled={!s.supported}>{s.name}{s.supported ? "" : ` (${s.timeframe}: not supported yet)`}</option>)}
            </select>
            <span className="hint">{strategy?.summary}</span></div>
          <div className="field"><label htmlFor="year">Year</label>
            <select id="year" value={/^\d{4}$/.test(year) ? year : "custom"} onChange={(e) => pickYear(e.target.value)}>
              {YEARS.map((y) => <option key={y} value={y}>{y}{y === FIRST_YEAR ? " (data starts in September)" : y === THIS_YEAR ? " (so far)" : ""}</option>)}
              <option value="custom">Custom dates</option>
            </select>
            <span className="hint">Picks 1 January to 31 December of that year. Strategies still read earlier candles to warm up.</span></div>
          <div className="field"><label htmlFor="label">Name this run (optional)</label><input id="label" type="text" maxLength={60} placeholder="e.g. 2% risk, best 20 stocks" value={f.label} onChange={set("label")} /></div>
          <div className="field"><label htmlFor="start">From</label><input id="start" type="date" value={f.start} onChange={setDate("start")} required /></div>
          <div className="field"><label htmlFor="end">To</label><input id="end" type="date" value={f.end} onChange={setDate("end")} required /></div>
          <div className="field quick-dates"><span className="hint">Quick dates</span><div className="row-flex">
            <button type="button" className={`chip${year === String(THIS_YEAR) ? " on" : ""}`} onClick={() => pickYear(String(THIS_YEAR))}>This year</button>
            <button type="button" className={`chip${year === "12m" ? " on" : ""}`} onClick={() => quick(isoDaysAgo(365), today(), "12m")}>Last 12 months</button>
            <button type="button" className={`chip${year === "since" ? " on" : ""}`} onClick={() => quick(monthly ? "2023-01-01" : "2022-01-01", today(), "since")}>{monthly ? "Since 2023 (all)" : "Since 2022 (all)"}</button>
          </div></div>
        </div>
        {strategy && monthly && (
          <p className="hint">Every 2 weeks (10 trading days) it ranks the chosen MCX commodities against each other on 12-month trend and carry (next contract cheaper), and holds the best 3 (with fewer than 6 chosen, the best half); a holding that stays in the top 3 is kept. Pick at least 2. History starts December 2021; the first trade is in January 2023 (it needs about a year of trend).</p>
        )}
        {strategy && gap && (
          <p className="hint">Intraday, buys only: the morning after a company's quarterly results, it buys at 09:30 if the stock opened 7% or more above the
            market and is still above its 09:15 opening price, and sells everything at 15:20. It skips stocks that were frozen at 09:30 or had under Rs50 lakh
            traded then. Its own list of 414 liquid NSE stocks; 5-minute data from September 2022 to mid-October 2026. Real Upstox intraday costs + 0.1% slippage
            each way are always charged.</p>
        )}
        {strategy && auto && !gap && (
          <p className="hint">This strategy chooses its own stocks: each day, the 200 most-traded NSE stocks of that day (about 4,250 checked, no fixed list).
            {strategy.name === "earnings_drift"
              ? " It buys a stock two days after quarterly results that beat the market by more than 4% on double volume, up to 5 at a time, and sells after 40 trading days. It buys only while half of those 200 stocks are above their 50-day average. Risk per trade does not apply."
              : strategy.name === "momentum_portfolio"
              ? " It holds the 15 with the best 6-month return / volatility, bought at the first open of the test and re-ranked after each quarter end (a holding stays while in the top 30). Risk per trade does not apply."
              : " Half the money buys quiet-day (NR7) breakouts, up to 5 stocks; half buys 55-day-high breakouts, up to 3 stocks. It buys only while at least half of those 200 stocks are above their 50-day average."}
            {" "}Daily data starts January 2022, so the first signals are in late 2022.</p>
        )}
        {strategy && crypto && (
          <p className="hint">5-minute dip-reversal on up to 11 coins' perpetual futures: buys a sharp one-sided dip (Bollinger-band squeeze or a slip under the lower band), exits at a 15% target, a 7% stop, or the price closing back above the middle band. 6% risk per trade -- worst fall seen was -75% on a continuously-compounding account (8% made only ~3-4% more for -88%); up to 5 positions open together. Real taker/maker fees + GST, slippage and funding included.</p>
        )}
        {strategy && !auto && (
          <div className="field">
            <label className="check" htmlFor="all"><input id="all" type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> All {strategy.symbols.length} symbols</label>
            {(strategy.default_symbols?.length ?? 0) > 0 && strategy.default_symbols.length < strategy.symbols.length && (
              <span className="hint">Its own list is ticked: the {strategy.default_symbols.length} symbols its paper trading uses
                {" "}(<button type="button" className="linkish" onClick={() => { setAll(false); setChosen(strategy.default_symbols); }}>tick only those again</button>). Tick more to test it on other stocks.</span>
            )}
            {!all && (
              <div className="symbols">
                {strategy.symbols.map((s) => <label key={s}><input type="checkbox" checked={chosen.includes(s)} onChange={() => toggle(s)} /> {s}</label>)}
              </div>
            )}
          </div>
        )}
      </Panel>

      <Panel title="Money, fees and slippage">
        <div className="form-grid">
          <div className="field"><label htmlFor="capital">Starting money (INR)</label><input id="capital" type="number" min="10000" max="1000000000" step="1" value={f.capital} onChange={set("capital")} /></div>
          {market === "equity" && !gap && (
            <div className="field"><label htmlFor="slip">Slippage per side (basis points)</label><input id="slip" type="number" min="0" max="100" step="0.5" value={f.slippage_bp} onChange={set("slippage_bp")} />
              <span className="hint">5 bp = 0.05%, charged when buying and again when selling.</span></div>
          )}
          {crypto ? (<>
            <div className="field"><label htmlFor="crisk">Risk per trade (% of account)</label>
              <input id="crisk" type="number" min="0.1" max="20" step="0.1" value={f.risk_pct} onChange={set("risk_pct")} />
              <span className="hint">Default 6%. Tested 2022-26: worst fall -75% (8%: -88%, 4%: -58%). Higher risk makes more on average but falls further in a bad stretch;
                position size scales directly with this, leverage below is just a safety ceiling.</span>
            </div>
            {!scalp && <div className="field"><label htmlFor="exposure">Leverage (x the account)</label>
              <input id="exposure" type="number" min={strategy.exposure_min ?? 1} max={strategy.exposure_max ?? 3.8} step="0.1" value={f.exposure} onChange={set("exposure")} />
              <span className="hint">Leverage multiple (capped at 3.8x max per trade to protect against gap liquidation; tested at 3.8x).</span>
            </div>}
            <div className="field"><label htmlFor="tax">Tax rule</label>
              <select id="tax" value={f.tax} onChange={(e) => setF((x) => ({ ...x, tax: e.target.value }))}>
                <option value="business">Business income (Section 43(5)): full compounding, 31.2% year-end net</option>
                <option value="none">No tax (before tax)</option>
                <option value="strict">Strict crypto rule: 31.2% flat VDA tax (worst case)</option>
              </select>
              <span className="hint">Crypto perpetual futures treated as business income under Sec 43(5) allows continuous compounding.</span></div>
          </>) : gap ? (
            <div className="field"><label htmlFor="exposure">Size (x the account)</label>
              <input id="exposure" type="number" min={strategy.exposure_min} max={strategy.exposure_max} step="0.5" value={f.exposure} onChange={set("exposure")} />
              <span className="hint">How much stock each morning buys, as a multiple of your money (Upstox intraday allows up to 5x), split across that
                morning's buys. The paper account uses {strategy.exposure}x. Tested 2022-26: 1x worst fall -8%, 2x -15%, 3x -22%, 5x -35%.</span>
              {+f.exposure > 3 && <span className="warn" role="alert">Above 3x a bad results day hurts a lot: at 5x the worst fall was -35%.</span>}</div>
          ) : monthly ? (
            <div className="field"><label htmlFor="exposure">Exposure (x the account)</label>
              <input id="exposure" type="number" min={strategy.exposure_min} max={strategy.exposure_max} step="0.25" value={f.exposure} onChange={set("exposure")} />
              <span className="hint">Total value of contracts held, as a multiple of your money: {strategy.exposure_min}x to {strategy.exposure_max}x (the most Upstox margin allows;
                each contract is also held to its own margin, at most 80% of the account in all). The paper account uses {strategy.exposure}x.</span>
              {+f.exposure > 2 && (
                <span className="warn" role="alert">
                  High exposure: profits and losses grow together. In June 2026 all three holdings fell ~15%: at 3x the worst fall was −44%, and at full Upstox
                  leverage (~8x) that one month lost about 85% of the account.
                </span>
              )}</div>
          ) : (<>
          <div className="field"><label htmlFor="risk">Risk per trade (% of account)</label><input id="risk" type="number" min="0.1" max="10" step="0.1" value={f.risk_pct} onChange={set("risk_pct")} />
            {+f.risk_pct > safeRisk && (
              <span className="warn" role="alert">
                High risk: one losing trade can cost about {(+f.risk_pct).toFixed(1)}% of the account, and positions get {(+f.risk_pct / (strategy?.default_risk_pct ?? 2)).toFixed(1)}x bigger than the tested {strategy?.default_risk_pct ?? 2}%.
                Profits AND losses grow together: at 10% the currency strategy made +64% in 2026 but lost 87% in 2013-22. Check other years before trusting it.
              </span>
            )}</div>
          {!auto && <div className="field"><label htmlFor="maxpos">Most holdings at once</label><input id="maxpos" type="number" min="1" max="10" value={f.max_positions} onChange={set("max_positions")} /></div>}
          </>)}
        </div>
        <p className="fees-box">
          {"Every trade is charged the full real costs, and fees cannot be switched off. Commodity (MCX): brokerage, CTT, stamp duty, exchange charges, GST and a half-spread on every trade, real month-end MCX prices including the monthly contract-switch cost; whole lots only. Crypto (Delta Exchange India perpetual futures): taker fee 0.05% + 18% GST + slippage on market orders, maker fee 0.02% + GST on limit take-profits, real funding; stops that gap are filled at the next price, not at the stop. Equity (NSE intraday): Upstox Rs30 per order, STT 0.025% on the sale, exchange, SEBI and stamp charges, GST, plus 0.1% slippage each way."}
        </p>
      </Panel>

      <div className="run-cta"><button className="main" type="submit" disabled={busy || !strategy?.supported}><Icon name="play" size={16} /> {busy ? "Running..." : "Run backtest"}</button><span className="note">Takes a few seconds to a minute; the result replays day by day.</span></div>
    </form>
  );
}
