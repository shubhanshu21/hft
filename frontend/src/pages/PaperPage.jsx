import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getPaper, resetPaper } from "../api/client.js";
import ResetAccount from "../components/paper/ResetAccount.jsx";
import Pane from "../components/ui/Pane.jsx";
import Pager from "../components/ui/Pager.jsx";
import { usePaged } from "../hooks/usePaged.js";
import { useTheme } from "../hooks/useTheme.js";
import { useCountUp } from "../hooks/useCountUp.js";
import { inr, signed, tone, whyText } from "../lib/format.js";
import { ACCOUNT_COLOR, ACCOUNT_SHORT } from "../lib/overview.js";

const STRATEGY_LABEL = { trend_carry: "MCX trend + carry", dip_reversal: "Crypto dip-reversal", results_gap: "Results-day gap-up" };
const ABOUT = {
  CRYPTO_DIP_REVERSAL: "Buys a sharp dip on 11 coins' perpetual futures, 5 seconds after every 5-minute candle closes. Exits at +15%, -7%, the middle band or after 24 hours.",
  MCX_TREND_CARRY: "Holds the best 3 of 8 MCX commodities on trend and carry, re-checked every 10 trading days at 09:10 or 23:40 IST.",
  RESULTS_GAP: "At 09:30 it buys liquid stocks that opened 7%+ above the market after results and kept rising, and sells them at 15:20.",
};
const when = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "-");
const qtyFmt = (q) => (q == null ? "-" : Number(q).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
const pxFmt = (p) => (p == null ? "-" : Number(p).toFixed(Math.abs(p) < 10 ? 4 : 2));   // small coin prices (DOGE 0.09) need more decimals
const ACCOUNTS = ["MCX_TREND_CARRY", "CRYPTO_DIP_REVERSAL", "RESULTS_GAP"];

/** Accounts (2026-10-29 layout redesign): one paper account -- a headline strip, the value chart and the engine card side by side,
 *  then blotters of open positions and closed trades. Read-only, refreshed every minute; no real orders are ever placed. */
export default function PaperPage({ accountId }) {
  const colors = useTheme();
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  const [loadedAt, setLoadedAt] = useState(null);
  const saved = () => { try { return localStorage.getItem("paperAccount"); } catch { return null; } };
  const account = ACCOUNTS.includes(accountId) ? accountId : ACCOUNTS.includes(saved()) ? saved() : "CRYPTO_DIP_REVERSAL";
  const pick = (id) => { window.location.hash = `#/paper/${id}`; };
  const box = useRef(null);
  const tradePaged = usePaged(d?.trades ?? [], 15);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    setD(null);
    try { localStorage.setItem("paperAccount", account); } catch { /* private window */ }
    const load = () => getPaper(account).then((x) => { setD(x); setError(""); setLoadedAt(new Date()); }).catch((e) => setError(e.message));
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [account, reloadKey]);

  useEffect(() => {
    if (!box.current || !d?.equity?.length) return undefined;
    const chart = createChart(box.current, baseOptions(colors, 260));
    const s = chart.addAreaSeries({ lineColor: colors.accent, topColor: "rgba(37,99,235,.22)", bottomColor: "rgba(37,99,235,0)", lineWidth: 2,
      priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` } });
    s.setData(d.equity.map((e) => ({ time: e.date, value: e.current_capital })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [d, colors]);

  const acc = d?.account ?? {};
  const open = (d?.positions ?? []).reduce((a, p) => a + (p.profit_now ?? 0), 0);
  const value = (acc.current_capital ?? 0) + open;
  const shown = useCountUp(d ? value : 0);

  const switcher = (
    <div className="seg" role="tablist" aria-label="Paper account">
      {ACCOUNTS.map((id) => <button key={id} type="button" role="tab" aria-selected={id === account} className={id === account ? "on" : ""} style={{ "--c": ACCOUNT_COLOR[id] }} onClick={() => pick(id)}><i />{ACCOUNT_SHORT[id]}</button>)}
    </div>
  );
  if (error) return <div className="acct-page">{switcher}<div className="panel error" role="alert">{error}</div></div>;
  if (!d) return <div className="acct-page">{switcher}<div className="panel none">Loading the paper account...</div></div>;
  if (!d.available) return <div className="panel none">There is no paper-trading account yet.</div>;

  const tab = d.accounts?.find((a) => a.id === d.account_id) ?? {};
  const gain = value - (acc.initial_capital ?? 0);
  const crypto = d.account_id.startsWith("CRYPTO");
  const noun = crypto ? "Coin" : d.account_id === "RESULTS_GAP" ? "Stock" : "Contract";
  const byStrategy = Object.values([...d.positions.map((p) => ({ k: p.strategy, openPnl: p.profit_now ?? 0, booked: 0 })),
    ...d.trades.map((t) => ({ k: t.strategy, openPnl: 0, booked: t.net_pnl ?? 0 }))].reduce((m, x) => {
    const r = m[x.k] ?? (m[x.k] = { name: x.k, label: STRATEGY_LABEL[x.k] ?? x.k, openPnl: 0, booked: 0 });
    r.openPnl += x.openPnl; r.booked += x.booked; return m;
  }, {}));
  return (
    <div className="acct-page" style={{ "--c": ACCOUNT_COLOR[d.account_id] }}>
      <div className="acct-head">
        <div className="ah-t"><i className="sw" /><div><h1>{tab.label ?? d.account_id}</h1><span className="ah-s">{tab.strategies} · {tab.market_text}</span></div></div>
        {switcher}
      </div>

      <section className="strip strip-6">
        <div className="cell lead"><span className="k">Account value</span><b className="big-v">{inr(shown)}</b></div>
        <div className="cell"><span className="k">Since start</span><b className={tone(gain)}>{signed(gain)}</b><small className={tone(gain)}>on {inr(acc.initial_capital ?? 0)}</small></div>
        <div className="cell"><span className="k">Open trades now</span><b className={tone(open)}>{signed(open)}</b><small>{d.positions.length} open</small></div>
        <div className="cell"><span className="k">Booked profit</span><b className={tone(d.stats.net)}>{signed(d.stats.net)}</b><small>{d.stats.closed ? `${d.stats.closed} sold, ${d.stats.wins} won` : "nothing sold yet"}</small></div>
        <div className="cell"><span className="k">Blocked as margin</span><b>{inr(acc.margin_used ?? 0)}</b><small>free {inr(acc.free_cash ?? 0)}</small></div>
        <div className="cell"><span className="k">Fees paid</span><b>{inr(d.stats.fees)}</b><small>on closed trades</small></div>
      </section>

      <div className="desk-grid">
        <Pane title="Account value by day" meta={loadedAt ? `updated ${loadedAt.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })} · every minute` : ""} className="g-chart">
          {d.equity.length ? <div ref={box} className="tv-box" /> : <p className="none">The first point appears after today's close.</p>}
        </Pane>
        <Pane title="Engine" meta={ACCOUNT_SHORT[d.account_id]} className="g-alloc">
          <div className={`engine-line ${d.engine?.ok === false ? "bad" : "good"}`}><i className={`led ${d.engine?.ok === false ? "bad" : "good"}`} />{d.engine?.text}</div>
          <p className="about">{ABOUT[d.account_id]}</p>
          <p className="note">Practice money, live prices from {crypto ? "Delta Exchange India" : "Upstox"}. Open trades are valued every minute; the account changes only when a trade is sold.</p>
          {acc.business_value != null && <p className="note">If all sold now: {inr(acc.pretax_value)} before tax · {inr(acc.business_value)} as business income · {inr(acc.strict_value)} under the strict crypto rule.</p>}
          <ResetAccount accountId={d.account_id} startMoney={acc.initial_capital} onReset={(acct, typed) => resetPaper(acct, typed).then((r) => { setReloadKey((k) => k + 1); return r; })} />
        </Pane>

        <Pane title={`Open positions (${d.positions.length})`} meta="profit now = the price move; if sold now also takes off fees and the buy-sell gap" className="g-full">
          {!d.positions.length ? <p className="none">Nothing open right now. {ABOUT[d.account_id]}</p> : (
            <div className="table-wrap"><table className="blotter">
              <thead><tr><th>{noun}</th><th>Size</th><th className="n">Bought</th><th className="n">Last price</th><th className="n">Buyers / sellers</th><th className="n">Move</th>
                <th className="n">Profit now</th><th className="n">If sold now</th><th className="n">{d.account_id === "RESULTS_GAP" ? "Money in it" : "Margin"}</th><th>Since</th></tr></thead>
              <tbody>{d.positions.map((p) => (
                <tr key={p.symbol} className="flash">
                  <td><b>{p.symbol}</b> <span className={`side ${p.direction === "short" ? "sell" : "buy"}`}>{p.direction === "short" ? "SELL" : "BUY"}</span></td>
                  <td>{p.size_text ?? qtyFmt(p.qty)}</td>
                  <td className="n">{pxFmt(p.entry_price)}</td>
                  <td className="n"><b>{pxFmt(p.price_now)}</b></td>
                  <td className="n">{p.bid != null || p.ask != null ? `${pxFmt(p.bid)} / ${pxFmt(p.ask)}` : "-"}</td>
                  <td className={`n ${p.move_pct == null ? "" : tone(p.move_pct)}`}>{p.move_pct == null ? "-" : `${p.move_pct >= 0 ? "+" : ""}${p.move_pct.toFixed(2)}%`}</td>
                  <td className={`n ${p.profit_now == null ? "" : tone(p.profit_now)}`}><b>{p.profit_now == null ? "-" : signed(p.profit_now)}</b></td>
                  <td className={`n ${p.net_if_sold == null ? "" : tone(p.net_if_sold)}`}>{p.net_if_sold == null ? "-" : signed(p.net_if_sold)}</td>
                  <td className="n">{inr(p.margin_used ?? 0)}</td>
                  <td>{when(p.entry_time)}</td>
                </tr>))}</tbody>
            </table></div>)}
        </Pane>

        <Pane title="Closed trades" meta={`${d.trades.length} · newest first`} className={byStrategy.length > 1 ? "g-chart" : "g-full"}>
          {!d.trades.length ? <p className="none">No closed trades yet. Finished trades and their profit after fees appear here.</p> : (
            <div className="table-wrap"><table className="blotter">
              <thead><tr><th>{noun}</th><th>Bought</th><th>Sold</th><th className="n">Qty</th><th className="n">Buy</th><th className="n">Sell</th><th className="n">Fees</th><th className="n">Profit after fees</th><th>Why it ended</th></tr></thead>
              <tbody>{tradePaged.rows.map((t, i) => <tr key={i}><td><b>{t.symbol}</b></td><td>{when(t.entry_dt)}</td><td>{when(t.exit_dt)}</td><td className="n">{qtyFmt(t.qty)}</td>
                <td className="n">{pxFmt(t.entry_price)}</td><td className="n">{pxFmt(t.exit_price)}</td><td className="n">{inr(t.total_friction ?? 0)}</td>
                <td className={`n ${tone(t.net_pnl ?? 0)}`}><b>{signed(t.net_pnl ?? 0)}</b></td><td>{whyText(t.exit_reason)}</td></tr>)}</tbody>
            </table><Pager paged={tradePaged} /></div>)}
        </Pane>
        {byStrategy.length > 1 && (
          <Pane title="By strategy" className="g-alloc">
            <table className="blotter"><thead><tr><th>Strategy</th><th className="n">Open</th><th className="n">Booked</th></tr></thead>
              <tbody>{byStrategy.map((r) => <tr key={r.name}><td>{r.label}</td><td className={`n ${tone(r.openPnl)}`}>{signed(r.openPnl)}</td><td className={`n ${tone(r.booked)}`}>{signed(r.booked)}</td></tr>)}</tbody></table>
          </Pane>
        )}
      </div>
    </div>
  );
}
