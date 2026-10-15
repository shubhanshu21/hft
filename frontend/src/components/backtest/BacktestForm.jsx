import { useEffect, useMemo, useState } from "react";
import Panel from "../ui/Panel.jsx";

const today = () => new Date().toISOString().slice(0, 10);
const THIS_YEAR = new Date().getFullYear();
const FIRST_YEAR = 2022;                    // the archived daily candles start in September 2022
const YEARS = Array.from({ length: THIS_YEAR - FIRST_YEAR + 1 }, (_, i) => THIS_YEAR - i);
const yearRange = (y) => ({ start: `${y}-01-01`, end: y === THIS_YEAR ? today() : `${y}-12-31` });
const MARKET_LABEL = { equity: "Equity (NIFTY 50 stocks)", currency: "Currency (NSE)", commodity: "Commodity (MCX futures)" };

export default function BacktestForm({ strategies, onSubmit, busy }) {
  const markets = useMemo(() => [...new Set(strategies.map((s) => s.market))], [strategies]);
  const [market, setMarket] = useState(markets[0] ?? "equity");
  const options = strategies.filter((s) => s.market === market);
  const [name, setName] = useState(options[0]?.name ?? "");
  const strategy = options.find((s) => s.name === name) ?? options[0];
  useEffect(() => { if (strategy) setF((x) => ({ ...x, risk_pct: strategy.default_risk_pct ?? 2, exposure: strategy.exposure ?? 1 })); }, [strategy?.market, strategy?.name]);   // start from the risk the strategy was tested with
  const safeRisk = Math.max(3, strategy?.default_risk_pct ?? 0);
  const monthly = !!strategy?.monthly_rank;          // commodity/trend_carry: ranks the chosen contracts together each month; exposure is editable
  const [all, setAll] = useState(true);
  const [chosen, setChosen] = useState([]);
  const [f, setF] = useState({ ...yearRange(THIS_YEAR), capital: 100000, slippage_bp: 5, risk_pct: 2, max_positions: 3, leverage: 1, label: "", exposure: 1.5 });
  const [year, setYear] = useState(String(THIS_YEAR));       // "custom" once the dates are edited by hand

  useEffect(() => { const o = strategies.filter((s) => s.market === market); setName(o[0]?.name ?? ""); setChosen([]); setAll(true);
    setF((x) => ({ ...x, slippage_bp: market === "currency" ? 0 : 5 }));   // currency: costs include the real spread; futures margin is ~2-3%
  }, [market, strategies]);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const pickYear = (v) => { setYear(v); if (v !== "custom") setF({ ...f, ...yearRange(Number(v)) }); };
  const setDate = (k) => (e) => { setYear("custom"); setF({ ...f, [k]: e.target.value }); };
  const toggle = (s) => setChosen(chosen.includes(s) ? chosen.filter((x) => x !== s) : [...chosen, s]);

  const submit = (e) => {
    e.preventDefault();
    onSubmit({
      market, strategy: name, symbols: all ? strategy.symbols : chosen,
      start: f.start, end: f.end, capital: +f.capital, slippage_bp: market === "currency" ? 0 : +f.slippage_bp, risk_pct: +f.risk_pct, max_positions: +f.max_positions,
      exposure: +f.exposure,
      label: f.label,   // exits, trailing stops and adding to winners are the strategy's own (the extra overrides were removed from the form)
    });
  };

  return (
    <form onSubmit={submit} className="wrap">
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
            <select id="year" value={year} onChange={(e) => pickYear(e.target.value)}>
              {YEARS.map((y) => <option key={y} value={y}>{y}{y === FIRST_YEAR ? " (data starts in September)" : y === THIS_YEAR ? " (so far)" : ""}</option>)}
              <option value="custom">Custom dates</option>
            </select>
            <span className="hint">Picks 1 January to 31 December of that year. Strategies still read earlier candles to warm up.</span></div>
          <div className="field"><label htmlFor="label">Name this run (optional)</label><input id="label" type="text" maxLength={60} placeholder="e.g. 2% risk, best 20 stocks" value={f.label} onChange={set("label")} /></div>
          <div className="field"><label htmlFor="start">From</label><input id="start" type="date" value={f.start} onChange={setDate("start")} required /></div>
          <div className="field"><label htmlFor="end">To</label><input id="end" type="date" value={f.end} onChange={setDate("end")} required /></div>
        </div>
        {strategy && monthly && (
          <p className="hint">Monthly strategy: every month end it ranks the chosen MCX commodities against each other and holds the best 3 (with fewer than 6 chosen,
            the best half). Pick at least 2. History starts December 2021; the first trade is in January 2023 (it needs 12 months of trend).</p>
        )}
        {strategy && (
          <div className="field">
            <label className="check" htmlFor="all"><input id="all" type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> All {strategy.symbols.length} symbols</label>
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
          {market === "equity" && (
            <div className="field"><label htmlFor="slip">Slippage per side (basis points)</label><input id="slip" type="number" min="0" max="100" step="0.5" value={f.slippage_bp} onChange={set("slippage_bp")} />
              <span className="hint">5 bp = 0.05%, charged when buying and again when selling.</span></div>
          )}
          {monthly ? (
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
          <div className="field"><label htmlFor="maxpos">Most holdings at once</label><input id="maxpos" type="number" min="1" max="10" value={f.max_positions} onChange={set("max_positions")} /></div>
          </>)}
        </div>
        <p className="fees-box">
          {"Every trade is charged the strategy's full cost model (brokerage, STT, exchange charges, GST, stamp duty, SEBI fees) and slippage on entry and exit (currency: the real bid-ask spread, built into its cost model). Stops that gap are filled at the open, not at the stop price. Fees cannot be switched off. Leverage is set for you: shares are bought with your own money (1x); currency futures use the margin Upstox really charges (about 34-41x), and the strategy caps itself at 20x in total. Commodity (MCX): brokerage, CTT, stamp duty, exchange charges, GST and a half-spread on every trade, real month-end MCX prices including the monthly contract-switch cost."}
        </p>
      </Panel>

      <div><button className="main" type="submit" disabled={busy || !strategy?.supported}>{busy ? "Running..." : "Run backtest"}</button></div>
    </form>
  );
}
