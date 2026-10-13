import { useEffect, useMemo, useState } from "react";
import Panel from "../ui/Panel.jsx";

const today = () => new Date().toISOString().slice(0, 10);
const THIS_YEAR = new Date().getFullYear();
const FIRST_YEAR = 2022;                    // the archived daily candles start in September 2022
const YEARS = Array.from({ length: THIS_YEAR - FIRST_YEAR + 1 }, (_, i) => THIS_YEAR - i);
const yearRange = (y) => ({ start: `${y}-01-01`, end: y === THIS_YEAR ? today() : `${y}-12-31` });
const MARKET_LABEL = { equity: "Equity (NIFTY 50 stocks)", currency: "Currency (NSE)", crypto: "Crypto (Binance)" };

export default function BacktestForm({ strategies, onSubmit, busy }) {
  const markets = useMemo(() => [...new Set(strategies.map((s) => s.market))], [strategies]);
  const [market, setMarket] = useState(markets[0] ?? "equity");
  const options = strategies.filter((s) => s.market === market);
  const [name, setName] = useState(options[0]?.name ?? "");
  const strategy = options.find((s) => s.name === name) ?? options[0];
  const [all, setAll] = useState(true);
  const [chosen, setChosen] = useState([]);
  const [f, setF] = useState({ ...yearRange(THIS_YEAR), capital: 100000, slippage_bp: 5, risk_pct: 2, max_positions: 3, leverage: 1, trail_stop_pct: 8, trail_after_pct: 5, profit_trail_pct: 0, pyramid_pct: "" });
  const [useTrail, setUseTrail] = useState(false);       // off: strategies bring their own exits (trend_ema_cross has a built-in trailing stop)
  const [year, setYear] = useState(String(THIS_YEAR));       // "custom" once the dates are edited by hand

  useEffect(() => { const o = strategies.filter((s) => s.market === market); setName(o[0]?.name ?? ""); setChosen([]); setAll(true);
    setF((x) => ({ ...x, slippage_bp: market === "currency" ? 0 : 5, leverage: market === "currency" ? 20 : 1 }));   // currency: costs include the real spread; futures margin is ~2-3%
  }, [market, strategies]);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const pickYear = (v) => { setYear(v); if (v !== "custom") setF({ ...f, ...yearRange(Number(v)) }); };
  const setDate = (k) => (e) => { setYear("custom"); setF({ ...f, [k]: e.target.value }); };
  const toggle = (s) => setChosen(chosen.includes(s) ? chosen.filter((x) => x !== s) : [...chosen, s]);
  const crypto = market === "crypto";

  const submit = (e) => {
    e.preventDefault();
    onSubmit({
      market, strategy: name, symbols: all ? strategy.symbols : chosen,
      start: f.start, end: f.end, capital: +f.capital, slippage_bp: +f.slippage_bp, risk_pct: +f.risk_pct, max_positions: +f.max_positions, leverage: +f.leverage,
      pyramid_pct: f.pyramid_pct === "" ? null : +f.pyramid_pct,
      trail_stop_pct: useTrail ? +f.trail_stop_pct : 0, trail_after_pct: useTrail ? +f.trail_after_pct : 0, profit_trail_pct: useTrail ? +f.profit_trail_pct : 0,
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
          <div className="field"><label htmlFor="start">From</label><input id="start" type="date" value={f.start} onChange={setDate("start")} required /></div>
          <div className="field"><label htmlFor="end">To</label><input id="end" type="date" value={f.end} onChange={setDate("end")} required /></div>
        </div>
        {strategy && (
          <div className="field">
            <label htmlFor="all"><input id="all" type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> All {strategy.symbols.length} symbols</label>
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
          <div className="field"><label htmlFor="capital">Starting money (INR)</label><input id="capital" type="number" min="10000" step="1000" value={f.capital} onChange={set("capital")} /></div>
          <div className="field"><label htmlFor="slip">Slippage per side (basis points)</label><input id="slip" type="number" min="0" max="100" step="0.5" value={f.slippage_bp} onChange={set("slippage_bp")} disabled={crypto} />
            <span className="hint">5 bp = 0.05%, charged when buying and again when selling. Currency: 0, because its cost model already includes the real bid-ask spread.</span></div>
          <div className="field"><label htmlFor="risk">Risk per trade (% of account)</label><input id="risk" type="number" min="0.1" max="10" step="0.1" value={f.risk_pct} onChange={set("risk_pct")} disabled={crypto} /></div>
          <div className="field"><label htmlFor="maxpos">Most holdings at once</label><input id="maxpos" type="number" min="1" max="10" value={f.max_positions} onChange={set("max_positions")} disabled={crypto} /></div>
          <div className="field"><label htmlFor="lev">Leverage</label><input id="lev" type="number" min="1" max="40" step="0.5" value={f.leverage} onChange={set("leverage")} disabled={crypto} />
            <span className="hint">Cash shares: 1. Currency futures: 20 (margin is about 2-3% of the contract).</span></div>
        </div>
        <p className="fees-box">
          {crypto
            ? "Crypto uses the live router's own costs: 0.12% per side on spot (SOL 0.15%), 0.09% on perpetuals, plus the real funding rate."
            : "Every trade is charged the strategy's full cost model (brokerage, STT, exchange charges, GST, stamp duty, SEBI fees) and the slippage above on entry and exit. Stops that gap are filled at the open, not at the stop price. Fees cannot be switched off."}
        </p>
      </Panel>

      <Panel title="Add to winning trades">
        <div className="form-grid">
          <div className="field"><label htmlFor="pyr">Buy a second lot when the trade is up (%)</label><input id="pyr" type="number" min="0" max="30" step="0.5" placeholder="strategy default" value={f.pyramid_pct} onChange={set("pyramid_pct")} disabled={crypto} />
            <span className="hint">Empty = the strategy's own setting. 0 = never. Both lots share the stop and are sold together; each buy pays its own fees.</span></div>
        </div>
      </Panel>

      <Panel title="Trailing stop and trailing profit">
        <label className="check" htmlFor="usetrail"><input id="usetrail" type="checkbox" checked={useTrail} onChange={(e) => setUseTrail(e.target.checked)} disabled={crypto} /> Add an extra trailing stop and trailing profit on top of the strategy's own exits (ready-made values below)</label>
        <div className="form-grid">
          <div className="field"><label htmlFor="tstop">Trailing stop (% below the best price)</label><input id="tstop" type="number" min="0" max="30" step="0.5" value={f.trail_stop_pct} onChange={set("trail_stop_pct")} disabled={crypto || !useTrail} />
            <span className="hint">Wide on purpose: a tight trail is hit by ordinary daily moves. The stop follows the price up and never moves down.</span></div>
          <div className="field"><label htmlFor="tafter">Start trailing after profit of (%)</label><input id="tafter" type="number" min="0" max="50" step="0.5" value={f.trail_after_pct} onChange={set("trail_after_pct")} disabled={crypto || !useTrail} />
            <span className="hint">0 = trail from the first day.</span></div>
          <div className="field"><label htmlFor="ptrail">Trailing profit (% give-back)</label><input id="ptrail" type="number" min="0" max="20" step="0.5" value={f.profit_trail_pct} onChange={set("profit_trail_pct")} disabled={crypto || !useTrail} />
            <span className="hint">0 = sell at the strategy's target (default: in tests, holding past the target lost money). Above 0 = at the target keep holding and sell when the price falls this % from its high.</span></div>
        </div>
      </Panel>

      <div><button className="main" type="submit" disabled={busy || !strategy?.supported}>{busy ? "Running..." : "Run backtest"}</button></div>
    </form>
  );
}
