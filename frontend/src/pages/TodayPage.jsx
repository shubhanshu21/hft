import { useEffect, useState } from "react";
import Icon from "../components/ui/Icon.jsx";
import Panel from "../components/ui/Panel.jsx";
import { getOverview } from "../api/client.js";
import { inr, signed, tone, whyText } from "../lib/format.js";
import { pctOf, statusTone } from "../lib/overview.js";

const ICON = { MCX_TREND_CARRY: "flame", CRYPTO_DIP_REVERSAL: "coins", RESULTS_GAP: "up" };
const when = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "-");
const pct = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

/** Every paper account on one screen: how much money there is, what happened today, and whether each engine is running. */
export default function TodayPage() {
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => getOverview().then((x) => { setD(x); setError(""); }).catch((e) => setError(e.message));
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);

  if (error && !d) return <div className="wrap"><div className="panel error" role="alert">{error}</div></div>;
  if (!d) return <div className="wrap"><div className="panel none">Loading your accounts...</div></div>;

  const t = d.totals;
  const gain = t.value - t.start;
  const problems = d.accounts.filter((a) => a.error || statusTone(a.engine) === "bad");
  return (
    <div className="wrap">
      <h1 className="sr-only">Today</h1>
      <section className="verdict today-verdict">
        <div className="v-main">
          <div className="v-eyebrow"><Icon name="wallet" size={15} /> All {d.accounts.length} paper accounts</div>
          <div className="v-big">{inr(t.value)}</div>
          <div className="v-chg"><span className={`v-delta ${gain >= 0 ? "is-up" : "is-down"}`}>{gain >= 0 ? "▲" : "▼"} {signed(gain)} ({pct(pctOf(t.value, t.start))})</span> since start ({inr(t.start)})</div>
          <div className={`engine-pill ${problems.length ? "bad" : "good"}`}><i />{problems.length ? `${problems.length} engine${problems.length > 1 ? "s need" : " needs"} attention` : "All engines running"}</div>
        </div>
        <div className="today-side">
          <div className="today-num"><span>Sold today + open now</span><b className={t.today_pnl >= 0 ? "is-up" : "is-down"}>{signed(t.today_pnl)}</b></div>
          <div className="today-num"><span>Open trades</span><b>{t.open_count}</b></div>
          <div className="v-note">Practice money, live prices. Updated {new Date(d.as_of).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}, refreshes every minute.</div>
        </div>
      </section>

      <Panel title="Accounts" className="ledger">
        {d.accounts.map((a) => (
          <a key={a.id} className="ledger-row" href={`#/paper/${a.id}`}>
            <span className="wallet-ico"><Icon name={ICON[a.id] ?? "chart"} size={20} /></span>
            <span className="lr-name">
              <b>{a.label}</b>
              <small>{a.strategy}{a.market_text ? ` · ${a.market_text}` : ""}</small>
            </span>
            {a.error ? <span className="lr-error">{a.label}: {a.error}</span> : <>
              <span className="lr-money">
                <b>{inr(a.value)}</b>
                <small className={tone(a.value - a.start)}>{pct(pctOf(a.value, a.start))} since start</small>
              </span>
              <span className="lr-today">
                <small>Sold today + open</small>
                <b className={a.today_pnl ? tone(a.today_pnl) : ""}>{a.today_pnl ? signed(a.today_pnl) : "₹0"}</b>
              </span>
              <span className="lr-open">
                {a.open.length ? a.open.map((p) => (
                  <span key={p.symbol} className="chip static">{p.symbol} <span className={p.profit_now == null ? "" : tone(p.profit_now)}>{p.profit_now == null ? "no price" : signed(p.profit_now)}</span></span>
                )) : <small>Nothing open</small>}
              </span>
              <span className={`lr-engine ${statusTone(a.engine)}`}><i />{a.engine?.text ?? "status unknown"}</span>
            </>}
            <span className="lr-go" aria-hidden="true">›</span>
          </a>
        ))}
      </Panel>

      <Panel title="Latest trades">
        {!d.recent.length ? <div className="empty"><span className="empty-ico"><Icon name="inbox" size={20} /></span><div><b>No trades yet</b>Every closed trade from all three accounts shows up here.</div></div> : (
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>Sold</th><th>Account</th><th>What</th><th>Profit after fees</th><th>Why it ended</th></tr></thead>
            <tbody>{d.recent.map((r, i) => <tr key={i}><td>{when(r.exit_dt)}</td><td className="ui">{r.account_label}</td><td>{r.symbol}</td>
              <td className={tone(r.net_pnl ?? 0)}>{signed(r.net_pnl ?? 0)}</td><td className="ui">{whyText(r.exit_reason)}</td></tr>)}</tbody>
          </table></div>)}
      </Panel>
    </div>
  );
}
