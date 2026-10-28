import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import Panel from "../components/ui/Panel.jsx";
import Stat from "../components/ui/Stat.jsx";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getPaper, resetPaper } from "../api/client.js";
import ResetAccount from "../components/paper/ResetAccount.jsx";
import Icon from "../components/ui/Icon.jsx";
import Sparkline from "../components/ui/Sparkline.jsx";
import OpenTradeCard from "../components/paper/OpenTradeCard.jsx";
import Pager from "../components/ui/Pager.jsx";
import { usePaged } from "../hooks/usePaged.js";
import { useTheme } from "../hooks/useTheme.js";
import { inr, signed, tone, whyText, walletAmount } from "../lib/format.js";

const STRATEGY_LABEL = { trend_carry: "MCX trend + carry", dip_reversal: "Crypto dip-reversal", results_gap: "Results-day gap-up" };
const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "-");
const qtyFmt = (q) => (q == null ? "-" : Number(q).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
const pxFmt = (p) => (p == null ? "-" : Number(p).toFixed(Math.abs(p) < 10 ? 4 : 2));   // small coin prices (DOGE 0.09) need more decimals

/** The paper-trading account as it is now (read-only, refreshed every minute). No real orders are ever placed. */
export default function PaperPage({ accountId }) {
  const colors = useTheme();
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  const [loadedAt, setLoadedAt] = useState(null);
  const [account, setAccount] = useState(() => { try { return accountId || localStorage.getItem("paperAccount") || "CRYPTO_DIP_REVERSAL"; } catch { return accountId || "CRYPTO_DIP_REVERSAL"; } });
  useEffect(() => { if (accountId && accountId !== account) pick(accountId); }, [accountId]);   // a link from the Today page (#/paper/<account>)
  const pick = (id) => {
    setAccount(id); setD(null);
    try { localStorage.setItem("paperAccount", id); window.history.replaceState(null, "", `#/paper/${id}`); } catch { /* private window */ }
  };
  const box = useRef(null);
  const tradePaged = usePaged(d?.trades ?? [], 15);

  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    const load = () => getPaper(account).then((x) => { setD(x); setError(""); setLoadedAt(new Date()); }).catch((e) => setError(e.message));
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [account, reloadKey]);

  useEffect(() => {
    if (!box.current || !d?.equity?.length) return undefined;
    const chart = createChart(box.current, baseOptions(colors, 240));
    const s = chart.addLineSeries({ color: colors.accent, lineWidth: 2, priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` } });
    s.setData(d.equity.map((e) => ({ time: e.date, value: e.current_capital })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [d, colors]);

  if (error) return <div className="wrap"><div className="panel error" role="alert">{error}</div></div>;
  if (!d) return <div className="wrap"><div className="panel none">Loading the paper account...</div></div>;
  if (!d.available) return <div className="wrap"><div className="panel none">There is no paper-trading account yet.</div></div>;

  const acc = d.account ?? {};
  const open = d.positions.reduce((a, p) => a + (p.profit_now ?? 0), 0);
  const value = (acc.current_capital ?? 0) + open;
  const live = d.positions.some((p) => p.price_source === "live");
  const byStrategy = Object.values([...d.positions.map((p) => ({ k: p.strategy, open: 1, openPnl: p.profit_now ?? 0, closed: 0, booked: 0 })),
    ...d.trades.map((t) => ({ k: t.strategy, open: 0, openPnl: 0, closed: 1, booked: t.net_pnl ?? 0 }))].reduce((m, x) => {
    const r = m[x.k] ?? (m[x.k] = { name: x.k, label: STRATEGY_LABEL[x.k] ?? x.k, open: 0, openPnl: 0, closed: 0, booked: 0 });
    r.open += x.open; r.openPnl += x.openPnl; r.closed += x.closed; r.booked += x.booked; return m;
  }, {}));
  return (
    <div className="wrap">
      {d.accounts?.length > 1 && (
        <div className="acct-tabs" role="tablist">
          {d.accounts.map((a) => (
            <button key={a.id} type="button" role="tab" aria-selected={a.id === d.account_id} className={a.id === d.account_id ? "on" : ""} onClick={() => pick(a.id)}>
              <span className="wallet-ico"><Icon name={a.id.startsWith("CRYPTO") ? "coins" : a.id === "RESULTS_GAP" ? "up" : "flame"} size={20} /></span>
              <span className="wallet-txt">
                <span className="t"><span className={`mkt-dot ${a.market_open ? "open" : "closed"}`} title={a.market_text} aria-label={a.market_text} />{a.label}</span>
                <span className="s">{a.strategies}{a.market_text ? ` · ${a.market_text}` : ""}</span>
              </span>
              {(() => { const w = walletAmount(a, a.id === d.account_id, value); return <span className="wallet-amt">{inr(w.amount)}<small>{w.note}</small></span>; })()}
            </button>
          ))}
        </div>
      )}
      <section className="verdict paper-verdict">
        <div className="v-main">
          <div className="v-eyebrow"><Icon name="wallet" size={15} /> Paper trading · {d.accounts?.find((a) => a.id === d.account_id)?.label ?? d.account_id}</div>
          <div className="v-big">{inr(value)}</div>
          <div className="v-chg"><span className={`v-delta ${value - acc.initial_capital >= 0 ? "is-up" : "is-down"}`}>{value - acc.initial_capital >= 0 ? "▲" : "▼"} {signed(value - acc.initial_capital)}</span> since start ({inr(acc.initial_capital)})</div>
          <h1 className="sr-only">Paper trading</h1>
          {d.engine?.text && <div className={`engine-pill ${d.engine.ok === false ? "bad" : "good"}`} title="Checked every minute; the same checks send a Telegram alert if the engine stops"><i />{d.engine.text}</div>}
          <div className="sub">Practice account: real prices, no real money. Open trades use the live {["CRYPTO_CALM", "CRYPTO_DIP_REVERSAL"].includes(d.account_id) ? "Delta Exchange India futures" : "Upstox"} price{live ? "" : " when it is available (otherwise the last close)"}.
            Updated {loadedAt ? loadedAt.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "-"}, refreshes every minute.</div>
        </div>
        <div className="v-chart">
          {d.equity?.length > 1 ? <Sparkline values={d.equity.map((e) => e.current_capital)} baseline={acc.initial_capital} height={150} />
            : <div className="v-chart-empty">The value line starts after the first full day.</div>}
          <ResetAccount accountId={d.account_id} startMoney={acc.initial_capital} onReset={(acct, typed) => resetPaper(acct, typed).then((r) => { setReloadKey((k) => k + 1); return r; })} />
          {acc.business_value != null && (
            <div className="v-note">If all sold now: {inr(acc.pretax_value)} before tax · {inr(acc.business_value)} after tax as business income · {inr(acc.strict_value)} under the strict crypto rule (worst case)</div>
          )}
        </div>
      </section>

      <Panel className="stats money">
        <Stat label="Account (booked)" value={inr(acc.current_capital ?? 0)} />
        <Stat label="Open trades now" value={<span className={tone(open)}>{signed(open)}</span>} />
        <Stat label="Blocked as margin" value={inr(acc.margin_used ?? 0)} />
        <Stat label="Free to trade" value={inr(acc.free_cash ?? 0)} />
        <Stat label="Booked profit" value={<span className={tone(d.stats.net)}>{signed(d.stats.net)}</span>} />
        <Stat label="Closed trades" value={d.stats.closed ? `${d.stats.closed} (${d.stats.wins} won)` : "0"} />
        <Stat label="Fees paid" value={inr(d.stats.fees)} />
      </Panel>

      <Panel title={`Open trades (${d.positions.length})`}>
        {!d.positions.length
          ? <div className="empty"><span className="empty-ico"><Icon name="sparkle" size={20} /></span><div><b>Nothing open right now</b>{{
            CRYPTO_DIP_REVERSAL: "dip_reversal checks all 11 coins for a sharp dip 5 seconds after every 5-minute candle closes.",
            MCX_TREND_CARRY: "trend_carry re-checks its picks every 2 weeks (10 trading days) and buys at the next 09:10 or 23:40 IST run on a weekday.",
            RESULTS_GAP: "Every weekday at 09:30 it checks stocks that just reported results: it buys any that opened 7%+ above the market and kept rising, and sells them at 15:20.",
          }[d.account_id] ?? "Waiting for the next signal."}</div></div>
          : <div className="trade-cards">{d.positions.map((p) => <OpenTradeCard key={p.symbol} p={p} />)}</div>}
        {d.positions.length > 0 && <p className="note">"Last traded price" changes only when someone trades. "Buyers / sellers now" are the best live offers: selling now would get the buyers' price. "Profit now" is the price move only. "If sold now" also takes off brokerage, taxes and the buy–sell price gap, the same way the paper trader books a sale.
          The account changes only when a trade is sold.</p>}
      </Panel>

      {byStrategy.length > 1 && (
        <Panel title="By strategy">
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>Strategy</th><th>Holding now</th><th>Open profit</th><th>Closed trades</th><th>Booked profit</th></tr></thead>
            <tbody>{byStrategy.map((r) => <tr key={r.name}><td>{r.label}</td><td>{r.open}</td><td className={tone(r.openPnl)}>{signed(r.openPnl)}</td>
              <td>{r.closed}</td><td className={tone(r.booked)}>{signed(r.booked)}</td></tr>)}</tbody>
          </table></div>
        </Panel>
      )}

      <Panel title="Closed trades">
        {!d.trades.length ? <div className="empty"><span className="empty-ico"><Icon name="inbox" size={20} /></span><div><b>No closed trades yet</b>Finished trades and their profit after fees will appear here.</div></div> : (
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>{d.account_id.startsWith("CRYPTO") ? "Coin" : d.account_id === "RESULTS_GAP" ? "Stock" : "Contract"}</th><th>Bought</th><th>Sold</th><th>Qty</th><th>Buy</th><th>Sell</th><th>Fees</th><th>Profit after fees</th><th>Why it ended</th></tr></thead>
            <tbody>{tradePaged.rows.map((t, i) => <tr key={i}><td>{t.symbol}</td><td>{when(t.entry_dt)}</td><td>{when(t.exit_dt)}</td><td>{qtyFmt(t.qty)}</td><td>{pxFmt(t.entry_price)}</td>
              <td>{pxFmt(t.exit_price)}</td><td>{inr(t.total_friction ?? 0)}</td><td className={tone(t.net_pnl ?? 0)}>{signed(t.net_pnl ?? 0)}</td><td>{whyText(t.exit_reason)}</td></tr>)}</tbody>
          </table><Pager paged={tradePaged} /></div>)}
      </Panel>

      <Panel title="Account value by day">{d.equity.length ? <div ref={box} className="tv-box" /> : <div className="none">The first point appears after today's close.</div>}</Panel>

    </div>
  );
}
