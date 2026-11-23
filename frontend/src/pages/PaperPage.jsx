import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getPaper, resetPaper } from "../api/client.js";
import ResetAccount from "../components/paper/ResetAccount.jsx";
import Pane from "../components/ui/Pane.jsx";
import Pager from "../components/ui/Pager.jsx";
import { usePaged } from "../hooks/usePaged.js";
import { useTheme, withAlpha } from "../hooks/useTheme.js";
import { useCountUp } from "../hooks/useCountUp.js";
import { inr, signed, tone, whyText } from "../lib/format.js";
import { ACCOUNT_COLOR, ACCOUNT_SHORT } from "../lib/overview.js";

const STRATEGY_LABEL = { dip_reversal: "Crypto dip-reversal", results_gap: "Results-day gap-up", nr7_breakout: "Quiet-day breakout", d55_breakout: "55-day high", momentum_12_1: "12-month momentum", earnings_drift: "Earnings drift (swing)" };
const ABOUT = {
  CRYPTO_DIP_REVERSAL: "Buys a sharp dip on 10 coins' perpetual futures, 5 seconds after every 5-minute candle closes -- but skips it when Binance open interest rose during the dip (new sellers piling in). Exits at +15%, -7%, the middle band or after 24 hours.",
  RESULTS_GAP: "Your one equity account: three strategies share it. Results gap (3x of the whole account, same day): at 09:30 it buys liquid stocks that opened 7%+ above the market after results and kept rising, and sells them at 15:20. Earnings drift (half the account): buys at the open stocks whose results jumped 4%+ vs the market on 2x volume, and sells them at the open 40 trading days later. Momentum (75% of the account, since 23 Nov 2026): on the first trading day of each month, the 20 stocks with the best 12-month return among the 300 most-traded, bought at the open and kept while they stay in the top 20. Swing holdings are cash only.",
  EQUITY_BREAKOUT: "Closed on 23 Nov 2026 (one equity account now): everything was sold at that day's open and it buys nothing; this page is history only. It ran two swing strategies, half the money each (since 19 Nov 2026). Breakouts on the day's 200 most-traded NSE stocks, only while at least half of them are above their 50-day average: a quarter of the money buys quiet-day (NR7) breakouts when the price passes that day's high (up to 5 stocks), a quarter buys stocks that closed at a 55-day high, at the next open (up to 3); each has a trailing stop, and one still below its buy price after 20 trading days is sold. Momentum: on the first trading day of each month, the 20 stocks with the best 12-month return among the 300 most-traded, bought at the open and kept while they stay in the top 20.",
};
const when = (s) => (s ? new Date(s).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "-");
const qtyFmt = (q) => (q == null ? "-" : Number(q).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
const pxFmt = (p) => (p == null ? "-" : Number(p).toFixed(Math.abs(p) < 10 ? 4 : 2));   // small coin prices (DOGE 0.09) need more decimals
const ACCOUNTS = ["CRYPTO_DIP_REVERSAL", "RESULTS_GAP", "EQUITY_BREAKOUT"];
const EQUITY = new Set(["RESULTS_GAP", "EQUITY_BREAKOUT"]);

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
    if (!box.current || (d?.equity?.length ?? 0) < 2) return undefined;
    const chart = createChart(box.current, baseOptions(colors, 260));
    const c = ACCOUNT_COLOR[d.account_id] ?? colors.accent;           // the account's own colour (blue is Breakout's)
    const s = chart.addAreaSeries({ lineColor: c, topColor: withAlpha(c, 0.22), bottomColor: withAlpha(c, 0), lineWidth: 2,
      priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` } });
    s.setData(d.equity.map((e) => ({ time: e.date, value: e.current_capital })));
    const exp = Object.entries(d.vs_backtest?.expected ?? {});
    if (exp.length) {                                              // the backtest over the same days, checked nightly
      const b = chart.addLineSeries({ color: colors.muted ?? "#64748b", lineWidth: 2, lineStyle: 2, priceLineVisible: false, lastValueVisible: false,
        priceFormat: { type: "custom", formatter: (v) => `₹${Math.round(v).toLocaleString("en-IN")}` } });
      b.setData(exp.map(([time, value]) => ({ time, value })));
    }
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.12, bottom: 0.08 } });   // the top price label was cut off
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [d, colors]);

  const acc = d?.account ?? {};
  const open = (d?.positions ?? []).reduce((a, p) => a + (p.profit_now ?? 0), 0);
  const value = (acc.current_capital ?? 0) + open;
  const shown = useCountUp(d ? value : 0);

  const switcher = (
    <div className="seg" role="tablist" aria-label="Paper account">
      {ACCOUNTS.filter((id) => !d?.accounts || d.accounts.some((a) => a.id === id)).map((id) => <button key={id} type="button" role="tab" aria-selected={id === account} className={id === account ? "on" : ""} style={{ "--c": ACCOUNT_COLOR[id] }} onClick={() => pick(id)}><i />{ACCOUNT_SHORT[id]}</button>)}
    </div>
  );
  if (error) return <div className="acct-page">{switcher}<div className="panel error" role="alert">{error}</div></div>;
  if (!d) return <div className="acct-page">{switcher}<div className="panel none">Loading the paper account...</div></div>;
  if (!d.available) return <div className="panel none">There is no paper-trading account yet.</div>;

  const tab = d.accounts?.find((a) => a.id === d.account_id) ?? {};
  const gain = value - (acc.initial_capital ?? 0);
  const crypto = d.account_id.startsWith("CRYPTO");
  const noun = crypto ? "Coin" : EQUITY.has(d.account_id) ? "Stock" : "Contract";
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
          {d.equity.length > 1 ? <div ref={box} className="tv-box" /> : <p className="none">The line starts after the account's second day; today it is {inr(d.equity[0]?.current_capital ?? acc.initial_capital ?? 0)}.</p>}
          {d.vs_backtest?.expected && <p className="chart-key"><i className="k-real" />this account (money booked from sold trades) <i className="k-bt" />its backtest over the same days, incl. open trades (checked nightly)</p>}
        </Pane>
        <Pane title="Engine" meta={ACCOUNT_SHORT[d.account_id]} className="g-alloc">
          <div className={`engine-line ${d.engine?.ok === false ? "bad" : "good"}`}><i className={`led ${d.engine?.ok === false ? "bad" : "good"}`} />{d.engine?.text}</div>
          <p className="about">{ABOUT[d.account_id]}</p>
          <p className="note">Practice money, live prices from {crypto ? "Delta Exchange India" : "Upstox"}. Open trades are valued every minute; the account changes only when a trade is sold.</p>
          {acc.business_value != null && <p className="note">If all sold now: {inr(acc.pretax_value)} before tax · {inr(acc.business_value)} as business income · {inr(acc.strict_value)} under the strict crypto rule.</p>}
          <ResetAccount accountId={d.account_id} startMoney={acc.initial_capital} onReset={(acct, typed) => resetPaper(acct, typed).then((r) => { setReloadKey((k) => k + 1); return r; })} />
        </Pane>

        <Pane title="Today" meta="what the engine decided this morning, and why" className="g-pos">
          {d.plan?.length ? <div className="today-plan"><ul>{d.plan.map((line) => <li key={line}>{line}</li>)}</ul></div>
            : <p className="none">{crypto ? "Checks all 10 coins after every 5-minute candle; a buy shows up under open positions." : "Nothing decided yet today -- the box fills after the engine's morning run."}</p>}
        </Pane>
        <Pane title="Paper vs backtest" meta="checked every night" className="g-sched">
          {d.vs_backtest ? <VsBacktest v={d.vs_backtest} /> : <p className="none">The first check runs tonight at 00:30.</p>}
        </Pane>

        <Pane title={`Open positions (${d.positions.length})`} meta="profit now = the price move; if sold now also takes off fees and the buy-sell gap" className="g-full">
          {!d.positions.length ? <p className="none">Nothing open right now. {ABOUT[d.account_id]}</p> : (
            <div className="table-wrap"><table className="blotter">
              <thead><tr><th>{noun}</th><th>Size</th><th className="n">Bought</th><th className="n">Last price</th><th className="n">Buyers / sellers</th><th className="n">Move</th>
                <th className="n">Profit now</th><th className="n">If sold now</th><th className="n">{EQUITY.has(d.account_id) ? "Money in it" : "Margin"}</th><th>Since</th></tr></thead>
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

const day = (s) => new Date(s).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
function VsBacktest({ v }) {
  if (!v) return null;
  if (v.note) return <p className="none">Not compared yet: {v.note}.</p>;
  const diff = [...v.only_paper.map((x) => `${x.sym} ${day(x.day)}: paper bought, backtest did not`),
    ...v.only_backtest.map((x) => `${x.sym} ${day(x.day)}: backtest bought, paper did not`)];
  return (
    <div className={`vs-bt ${diff.length ? "warn" : "ok"}`}>
      <span className="k">Since {day(v.start)}</span>
      <p>{v.matched} buy{v.matched === 1 ? "" : "s"} match{v.matched === 1 ? "es" : ""}{v.avg_fill_gap_pct != null ? `, fills ${v.avg_fill_gap_pct >= 0 ? "+" : ""}${v.avg_fill_gap_pct}% vs the backtest on average` : ""}.
        {diff.length ? ` ${diff.length} differ:` : " Nothing differs."}</p>
      {diff.length > 0 && <ul>{diff.map((t) => <li key={t}>{t}</li>)}</ul>}
    </div>
  );
}
