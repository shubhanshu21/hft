import { useEffect, useMemo, useState } from "react";
import Panel from "../ui/Panel.jsx";
import { getToken, setToken } from "../../api/client.js";

const today = () => new Date().toISOString().slice(0, 10);
const MARKET_LABEL = { equity: "Equity (NIFTY 50 stocks)", currency: "Currency (NSE)", crypto: "Crypto (Binance)" };

export default function BacktestForm({ strategies, onSubmit, busy }) {
  const markets = useMemo(() => [...new Set(strategies.map((s) => s.market))], [strategies]);
  const [market, setMarket] = useState(markets[0] ?? "equity");
  const options = strategies.filter((s) => s.market === market);
  const [name, setName] = useState(options[0]?.name ?? "");
  const strategy = options.find((s) => s.name === name) ?? options[0];
  const [all, setAll] = useState(true);
  const [chosen, setChosen] = useState([]);
  const [f, setF] = useState({ start: "2023-01-01", end: today(), capital: 100000, slippage_bp: 5, risk_pct: 2, max_positions: 3, leverage: 1 });
  const [token, setTok] = useState(getToken());

  useEffect(() => { const o = strategies.filter((s) => s.market === market); setName(o[0]?.name ?? ""); setChosen([]); setAll(true); }, [market, strategies]);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const toggle = (s) => setChosen(chosen.includes(s) ? chosen.filter((x) => x !== s) : [...chosen, s]);
  const crypto = market === "crypto";

  const submit = (e) => {
    e.preventDefault();
    setToken(token);
    onSubmit({
      market, strategy: name, symbols: all ? strategy.symbols : chosen,
      start: f.start, end: f.end, capital: +f.capital, slippage_bp: +f.slippage_bp, risk_pct: +f.risk_pct, max_positions: +f.max_positions, leverage: +f.leverage,
    }, token);
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
          <div className="field"><label htmlFor="start">From</label><input id="start" type="date" value={f.start} onChange={set("start")} required /></div>
          <div className="field"><label htmlFor="end">To</label><input id="end" type="date" value={f.end} onChange={set("end")} required /></div>
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
            <span className="hint">5 bp = 0.05%. Charged when buying and again when selling.</span></div>
          <div className="field"><label htmlFor="risk">Risk per trade (% of account)</label><input id="risk" type="number" min="0.1" max="10" step="0.1" value={f.risk_pct} onChange={set("risk_pct")} disabled={crypto} /></div>
          <div className="field"><label htmlFor="maxpos">Most holdings at once</label><input id="maxpos" type="number" min="1" max="10" value={f.max_positions} onChange={set("max_positions")} disabled={crypto} /></div>
          <div className="field"><label htmlFor="lev">Leverage</label><input id="lev" type="number" min="1" max="10" step="0.5" value={f.leverage} onChange={set("leverage")} disabled={crypto} />
            <span className="hint">Cash shares have none: keep 1.</span></div>
        </div>
        <p className="fees-box">
          {crypto
            ? "Crypto uses the live router's own costs: 0.12% per side on spot (SOL 0.15%), 0.09% on perpetuals, plus the real funding rate."
            : "Every trade is charged the strategy's full cost model (brokerage, STT, exchange charges, GST, stamp duty, SEBI fees) and the slippage above on entry and exit. Stops that gap are filled at the open, not at the stop price. Fees cannot be switched off."}
        </p>
      </Panel>

      <Panel title="Access">
        <div className="form-grid">
          <div className="field"><label htmlFor="token">Access token</label><input id="token" type="password" autoComplete="off" value={token} onChange={(e) => setTok(e.target.value)} required />
            <span className="hint">Set as BACKTEST_TOKEN on the server. Kept in this browser only.</span></div>
        </div>
      </Panel>
      <div><button className="main" type="submit" disabled={busy || !strategy?.supported}>{busy ? "Running..." : "Run backtest"}</button></div>
    </form>
  );
}
