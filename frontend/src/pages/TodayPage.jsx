import { useEffect, useRef, useState } from "react";
import { getPaper } from "../api/client.js";
import { inr, signed, tone, whyText } from "../lib/format.js";
import { ACCOUNT_COLOR, ACCOUNT_SHORT, donutSlices, pctOf, statusTone } from "../lib/overview.js";
import { minutesUntil, untilText } from "../lib/palette.js";
import { useCountUp } from "../hooks/useCountUp.js";
import { useOverview } from "../hooks/useOverview.js";
import Sparkline from "../components/ui/Sparkline.jsx";
import Pane from "../components/ui/Pane.jsx";

const WHAT = { MCX_TREND_CARRY: "Commodity futures", CRYPTO_DIP_REVERSAL: "Perpetual futures", RESULTS_GAP: "NSE intraday + swing" };
const when = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "-");

/** Desk (2026-10-29 layout redesign): a headline strip, then a dense grid -- value chart, allocation, open positions, engine schedule,
 *  accounts and the latest fills -- like a trading desk's home screen. */
export default function TodayPage() {
  const { data: d, error } = useOverview();
  const [curve, setCurve] = useState(null);
  const [flash, setFlash] = useState(0);
  const last = useRef("");
  useEffect(() => {
    if (!d) return;
    const sig = d.accounts.map((a) => a.value).join();
    if (last.current && sig !== last.current) setFlash((f) => f + 1);
    last.current = sig;
  }, [d]);
  useEffect(() => {                                            // the value line: each account's daily values added up (once per visit)
    if (!d || curve) return;
    Promise.all(d.accounts.map((a) => getPaper(a.id).catch(() => ({})))).then((rs) => {
      const lines = d.accounts.map((a, i) => [a.start, ...(rs[i].equity ?? []).map((e) => e.current_capital), a.value]);
      const n = Math.max(...lines.map((l) => l.length));
      setCurve(Array.from({ length: n }, (_, k) => lines.reduce((s, l) => s + l[Math.min(k, l.length - 1)], 0)));
    });
  }, [d, curve]);
  const total = useCountUp(d?.totals.value ?? 0);

  if (error && !d) return <div className="panel error" role="alert">{error}</div>;
  if (!d) return <div className="panel none">Loading your accounts...</div>;
  const t = d.totals, gain = t.value - t.start;
  const ok = d.accounts.filter((a) => !a.error);
  const problems = d.accounts.filter((a) => a.error || statusTone(a.engine) === "bad");
  const positions = ok.flatMap((a) => a.open.map((p) => ({ ...p, acct: a })));
  const slices = donutSlices(d.accounts);
  const R = 52, C = 2 * Math.PI * R;
  const schedule = ok.map((a) => ({ a, mins: minutesUntil(a.engine?.text) })).sort((x, y) => (x.mins ?? 1e9) - (y.mins ?? 1e9));
  return (
    <div className="desk">
      <h1 className="sr-only">Desk</h1>
      <section className="strip">
        <div className="cell lead"><span className="k">Total value · 3 accounts</span><b className="big-v">{inr(total)}</b></div>
        <div className="cell"><span className="k">Since start</span><b className={tone(gain)}>{signed(gain)}</b><small className={tone(gain)}>{gain >= 0 ? "▲" : "▼"} {Math.abs(pctOf(t.value, t.start)).toFixed(2)}% on {inr(t.start)}</small></div>
        <div className="cell"><span className="k">Today</span><b className={t.today_pnl ? tone(t.today_pnl) : ""}>{signed(t.today_pnl)}</b><small>{d.recent.filter((r) => String(r.exit_dt).slice(0, 10) === d.as_of.slice(0, 10)).length} sold today</small></div>
        <div className="cell"><span className="k">Open positions</span><b>{t.open_count}</b><small>{positions.length ? positions.map((p) => p.symbol).join(", ") : "none"}</small></div>
        <div className="cell"><span className="k">Engines</span><b className={problems.length ? "down" : "up"}><span className={`pulse ${problems.length ? "bad" : ""}`} />{problems.length ? `${problems.length} down` : `${ok.length} / ${d.accounts.length} up`}</b><small>checked every 15 min</small></div>
      </section>

      <div className="desk-grid">
        <Pane title="Portfolio value" meta={`all accounts · since ${inr(t.start)} start`} className="g-chart">
          {curve && curve.length > 1 ? <Sparkline className="grow" values={curve} baseline={t.start} height={230} color="#2563eb" />
            : <p className="none">The line fills in day by day.</p>}
        </Pane>

        <Pane title="Allocation" meta="share of money" className="g-alloc">
          <div className="donut">
            <svg viewBox="0 0 150 150" role="img" aria-label="Share of money in each account">
              <circle cx="75" cy="75" r={R} className="donut-bg" />
              {slices.map((s) => <circle key={s.id} cx="75" cy="75" r={R} className="donut-seg" stroke={ACCOUNT_COLOR[s.id]}
                strokeDasharray={`${Math.max(0, s.share * C - 3)} ${C}`} strokeDashoffset={-s.start * C} transform="rotate(-90 75 75)" />)}
              <text x="75" y="72" textAnchor="middle" className="donut-v">{slices.length}</text>
              <text x="75" y="90" textAnchor="middle" className="donut-l">accounts</text>
            </svg>
            <div className="legend">{slices.map((s) => {
              const a = d.accounts.find((x) => x.id === s.id);
              return <div key={s.id}><i style={{ background: ACCOUNT_COLOR[s.id] }} /><span className="lg-n">{ACCOUNT_SHORT[s.id]}<small>{inr(a.value)}</small></span><span>{Math.round(s.share * 100)}%</span></div>;
            })}</div>
          </div>
        </Pane>

        <Pane title="Open positions" meta={`${positions.length} live · prices every minute`} className="g-pos">
          {!positions.length ? <p className="none">Nothing open. Each engine buys on its own signal; the schedule shows when each one looks next.</p> : (
            <div className="table-wrap"><table className="blotter">
              <thead><tr><th>Symbol</th><th>Account</th><th className="n">Move</th><th className="n">Profit now</th></tr></thead>
              <tbody>{positions.map((p) => (
                <tr key={`${p.acct.id}-${p.symbol}-${flash}`} className="flash" onClick={() => { window.location.hash = `#/paper/${p.acct.id}`; }}>
                  <td><b>{p.symbol}</b></td>
                  <td><span className="tag" style={{ "--c": ACCOUNT_COLOR[p.acct.id] }}>{ACCOUNT_SHORT[p.acct.id]}</span></td>
                  <td className={`n ${p.move_pct == null ? "" : tone(p.move_pct)}`}>{p.move_pct == null ? "-" : `${p.move_pct >= 0 ? "+" : ""}${p.move_pct.toFixed(2)}%`}</td>
                  <td className={`n ${p.profit_now == null ? "" : tone(p.profit_now)}`}><b>{p.profit_now == null ? "no price" : signed(p.profit_now)}</b></td>
                </tr>))}</tbody>
            </table></div>)}
        </Pane>

        <Pane title="Engine schedule" meta="next check" className="g-sched">
          <ul className="sched">{schedule.map(({ a, mins }) => (
            <li key={a.id}><i className={`led ${statusTone(a.engine)}`} /><span>{ACCOUNT_SHORT[a.id]}<small>{WHAT[a.id]}</small></span>
              <b>{(String(a.engine?.text).match(/next at ([^·]+)/) || [])[1]?.replace(" IST", "") ?? "-"}<small>{untilText(mins)}</small></b></li>))}
          </ul>
        </Pane>

        <Pane title="Accounts" meta="click a row to open it" className="g-accts">
          <div className="table-wrap"><table className="blotter">
            <thead><tr><th>Account</th><th className="n">Value</th><th className="n">Since start</th><th className="n">Today</th><th className="n">Open</th><th>Market</th></tr></thead>
            <tbody>{d.accounts.map((a) => {
              if (a.error) return <tr key={a.id}><td>{a.label}</td><td colSpan={5} className="down">{a.error}</td></tr>;
              const pc = pctOf(a.value, a.start);
              return (
                <tr key={`${a.id}-${flash}`} className="flash" onClick={() => { window.location.hash = `#/paper/${a.id}`; }}>
                  <td><a className="acct" href={`#/paper/${a.id}`}><i style={{ background: ACCOUNT_COLOR[a.id] }} /><span>{a.label}<small>{WHAT[a.id]}</small></span></a></td>
                  <td className="n"><b>{inr(a.value)}</b></td>
                  <td className="n"><span className={tone(pc)}>{pc >= 0 ? "+" : ""}{pc.toFixed(1)}%</span>
                    <span className="bar"><span style={{ width: `${Math.min(100, Math.abs(pc) * 3)}%`, background: pc >= 0 ? "var(--gain)" : "var(--loss)" }} /></span></td>
                  <td className={`n ${a.today_pnl ? tone(a.today_pnl) : ""}`}>{a.today_pnl ? signed(a.today_pnl) : "₹0"}</td>
                  <td className="n">{a.open_count}</td>
                  <td><span className={`mk ${a.market_open ? "open" : "closed"}`}><i />{a.market_open ? "Open" : "Closed"}</span></td>
                </tr>);
            })}</tbody>
          </table></div>
        </Pane>

        <Pane title="Latest fills" meta="sold trades, all accounts" className="g-fills">
          {!d.recent.length ? <p className="none">No closed trades yet.</p> : (
            <ul className="fills">{d.recent.slice(0, 8).map((r, i) => (
              <li key={i}><span className="tag" style={{ "--c": ACCOUNT_COLOR[r.account] }}>{ACCOUNT_SHORT[r.account]}</span>
                <span className="f-s">{r.symbol}<small>{when(r.exit_dt)} · {whyText(r.exit_reason)}</small></span>
                <b className={tone(r.net_pnl ?? 0)}>{signed(r.net_pnl ?? 0)}</b></li>))}
            </ul>)}
        </Pane>
      </div>
    </div>
  );
}
