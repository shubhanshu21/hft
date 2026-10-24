import Stat from "../ui/Stat.jsx";
import Icon from "../ui/Icon.jsx";
import Sparkline from "../ui/Sparkline.jsx";
import Bridge from "./Bridge.jsx";
import { inr, pct, shortDate, signed, tone } from "../../lib/format.js";

/** The steps from the starting money to what you keep: [{ label, hint, kind, lo, hi, amount }] on one shared rupee scale.
 *  Crypto runs carry final_pretax (the gap to final_equity is the income tax); MCX runs have no tax line.
 *  2026-10-24: replaces the old "Costs" card, which mixed before-tax and after-tax numbers and confused the user. */
export function moneySteps(m) {
  if (m.gross_profit == null || m.start_capital == null) return [];
  const S = m.start_capital, G = m.gross_profit, slip = m.slippage ?? 0;
  const pre = m.final_pretax ?? m.final_equity;
  const fees = m.final_pretax != null ? G - (pre - S) - slip : (m.fees ?? 0);
  const tax = pre - m.final_equity;
  const steps = [{ label: "You started with", short: "Started", kind: "base", lo: 0, hi: S, amount: inr(S) }];
  let x = S;
  steps.push({ label: "Profit from price moves", short: "Price moves", hint: "what the trades earned before fees", kind: G >= 0 ? "gain" : "loss", lo: Math.min(x, x + G), hi: Math.max(x, x + G), amount: signed(G) });
  x += G;
  steps.push({ label: "Fees + funding paid", short: "Fees", hint: "exchange fees, GST, funding", kind: "loss", lo: x - fees, hi: x, amount: signed(-fees) });
  x -= fees;
  if (slip > 0.5) { steps.push({ label: "Slippage", short: "Slippage", hint: "worse fills than the quoted price", kind: "loss", lo: x - slip, hi: x, amount: signed(-slip) }); x -= slip; }
  if (tax > 0.5) {
    steps.push({ label: "Your money before income tax", short: "Before tax", kind: "base", lo: 0, hi: pre, amount: inr(pre) });
    steps.push({ label: "Income tax", short: "Tax", hint: m.tax_mode === "strict" ? "strict crypto rule" : "31.2% as business income", kind: "loss", lo: pre - tax, hi: pre, amount: signed(-tax) });
  }
  steps.push({ label: "You keep", short: "You keep", kind: "total", lo: 0, hi: m.final_equity, amount: inr(m.final_equity), final: true });
  return steps;
}

function Waterfall({ steps }) {
  const max = Math.max(...steps.map((s) => s.hi), 1);
  return (
    <div className="wf" role="table" aria-label="Where the money went, step by step">
      {steps.map((s) => (
        <div className={`wf-row${s.final ? " final" : ""}`} role="row" key={s.label}>
          <div className="wf-label" role="rowheader">{s.label}{s.hint && <small>{s.hint}</small>}</div>
          <div className="wf-track" aria-hidden="true" title={`${s.label}: ${s.amount}`}>
            <div className={`wf-bar ${s.kind}`} style={{ left: `${(s.lo / max) * 100}%`, width: `${((s.hi - s.lo) / max) * 100}%` }} />
          </div>
          <div className={`wf-amt ${s.kind === "gain" ? "up" : s.kind === "loss" ? "down" : ""}`} role="cell">{s.amount}</div>
        </div>
      ))}
    </div>
  );
}

/** The whole-run verdict, shown before the replay: key-number tiles (icon + value + a tiny trend), the money bridge, then trade facts. */
export default function MetricsGrid({ metrics: m, best = null, worst = null, equity = null }) {
  const pf = m.profit_factor == null ? "-" : m.profit_factor === Infinity || m.profit_factor === "Infinity" ? "no losers" : m.profit_factor;
  const taxed = m.final_pretax != null && Math.abs(m.final_pretax - m.final_equity) > 0.5;
  const steps = moneySteps(m);
  let peak = -Infinity;
  const dd = (equity ?? []).map((v) => { peak = Math.max(peak, v); return peak > 0 ? (v / peak - 1) * 100 : 0; });
  const win = m.trades ? m.win_rate_pct : null;
  const kpis = [
    { icon: "wallet", label: "You keep", value: inr(m.final_equity), lead: true,
      extra: equity?.length > 1 && <Sparkline values={equity} baseline={m.start_capital} height={34} className="kpi-spark" /> },
    { icon: "percent", label: taxed ? "Return after tax" : "Return", value: <span className={tone(m.return_pct)}>{pct(m.return_pct, 1)}</span>,
      extra: <div className="kpi-foot">on {inr(m.start_capital)}</div> },
    { icon: "calendar", label: "Per year", value: <span className={tone(m.cagr_pct)}>{pct(m.cagr_pct, 1)}</span>, extra: <div className="kpi-foot">yearly growth rate (CAGR)</div> },
    { icon: "down", label: "Worst fall from a peak", value: <span className={m.max_drawdown_pct < 0 ? "down" : ""}>{m.max_drawdown_pct}%</span>,
      extra: dd.length > 1 && <Sparkline values={dd} baseline={0} height={34} className="kpi-spark loss" /> },
    { icon: "trophy", label: "Trades won", value: win != null ? <>{win}%<span className="v-sub">{m.wins} of {m.trades}</span></> : "-",
      extra: win != null && <div className="meter" role="img" aria-label={`${win}% of trades won`}><span style={{ width: `${win}%` }} /></div> },
  ];
  const facts = [
    ["Closed trades", m.trades],
    ["Profit factor", <>{pf}<span className="v-sub">won ÷ lost</span></>],
    ["Average hold", m.avg_hold_days != null ? (m.avg_hold_days < 1 ? `${Math.max(1, Math.round(m.avg_hold_days * 24))} hours` : `${m.avg_hold_days} days`) : "-"],
    ...(best && best.pnl > 0 ? [["Best trade", <><span className="up">{signed(best.pnl)}</span><span className="v-sub">{best.sym} · {shortDate(best.day)}</span></>]] : []),
    ...(worst && worst.pnl < 0 ? [["Worst trade", <><span className="down">{signed(worst.pnl)}</span><span className="v-sub">{worst.sym} · {shortDate(worst.day)}</span></>]] : []),
    ...(m.open_at_end ? [[`Not sold yet (${m.open_at_end} open)`, <span className={tone(m.open_profit)}>{signed(m.open_profit)}</span>]] : []),
  ];
  return (
    <section className="metrics-wrap" aria-label="Whole run, after fees and slippage">
      <div className="kpis">
        {kpis.map((k) => (
          <div className={`kpi${k.lead ? " lead" : ""}`} key={k.label}>
            <div className="kpi-top"><span className="kpi-ico"><Icon name={k.icon} size={16} /></span><span className="kpi-l">{k.label}</span></div>
            <div className="kpi-v">{k.value}</div>
            {k.extra}
          </div>
        ))}
      </div>
      <div className="metrics">
        {steps.length > 0 && (
          <div className="metric-group">
            <div className="card-head"><span className="head-ico"><Icon name="receipt" size={16} /></span><h2>Where the money went</h2></div>
            <div className="bridge-wrap"><Bridge steps={steps} /></div>
            <details className="as-list"><summary>Show as a list</summary><Waterfall steps={steps} /></details>
          </div>
        )}
        <div className="metric-group">
          <div className="card-head"><span className="head-ico"><Icon name="layers" size={16} /></span><h2>Trades</h2></div>
          <div className="metric-grid">{facts.map(([l, v]) => <Stat key={l} label={l} value={v} />)}</div>
        </div>
      </div>
      {m.note && m.final_pretax == null && <p className="callout">{m.note}</p>}
    </section>
  );
}
