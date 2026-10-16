import { useEffect, useRef, useState } from "react";
import { createChart } from "lightweight-charts";
import Panel from "../components/ui/Panel.jsx";
import Stat from "../components/ui/Stat.jsx";
import { baseOptions } from "../components/charts/chartOptions.js";
import { getPaper } from "../api/client.js";
import StrategyStatus from "../components/paper/StrategyStatus.jsx";
import OpenTradeCard from "../components/paper/OpenTradeCard.jsx";
import Pager from "../components/ui/Pager.jsx";
import { usePaged } from "../hooks/usePaged.js";
import { useTheme } from "../hooks/useTheme.js";
import { inr, signed, tone } from "../lib/format.js";

const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "-");

/** The paper-trading account as it is now (read-only, refreshed every minute). No real orders are ever placed. */
export default function PaperPage() {
  const colors = useTheme();
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  const [loadedAt, setLoadedAt] = useState(null);
  const [account, setAccount] = useState(() => { try { return localStorage.getItem("paperAccount") || "DRYRUN_ACCOUNT"; } catch { return "DRYRUN_ACCOUNT"; } });
  const pick = (id) => { setAccount(id); setD(null); try { localStorage.setItem("paperAccount", id); } catch { /* private window */ } };
  const box = useRef(null);
  const tradePaged = usePaged(d?.trades ?? [], 15);

  useEffect(() => {
    const load = () => getPaper(account).then((x) => { setD(x); setError(""); setLoadedAt(new Date()); }).catch((e) => setError(e.message));
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [account]);

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
  return (
    <div className="wrap">
      {d.accounts?.length > 1 && (
        <div className="acct-tabs" role="tablist">
          {d.accounts.map((a) => (
            <button key={a.id} type="button" role="tab" aria-selected={a.id === d.account_id} className={a.id === d.account_id ? "on" : ""} onClick={() => pick(a.id)}>
              <span className="t">{a.label}</span>
              <span className="s">{inr(a.booked)} · {a.open} open</span>
            </button>
          ))}
        </div>
      )}
      <div className="page-head">
        <div>
          <h1>Paper trading</h1>
          <div className="sub">Practice account: real prices, no real money. Open trades use the live Upstox price{live ? "" : " when it is available (otherwise the last close)"}.
            Updated {loadedAt ? loadedAt.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "-"}, refreshes every minute.</div>
        </div>
        <div className="big">
          <div className="eq">{inr(value)}</div>
          <div className="chg"><span className={tone(value - acc.initial_capital)}>{signed(value - acc.initial_capital)}</span> since start ({inr(acc.initial_capital)})</div>
        </div>
      </div>

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
          ? <div className="none">{{
            MCX_TREND_CARRY: "No open trades. trend_carry rebalances at 09:10 on the first MCX trading day of each month.",
            EQUITY_BREAKOUT: "No open trades. Breakout buys only while at least half of the 200 most-traded stocks are above their 50-day average: 55-day highs at the 9:15 open, quiet-day breakouts during the day.",
          }[d.account_id] ?? "No open trades. Equity buys only between 9:15 and 10:00 when its rules are met; currency checks at the 9:00 open."}</div>
          : <div className="trade-cards">{d.positions.map((p) => <OpenTradeCard key={p.symbol} p={p} />)}</div>}
        {d.positions.length > 0 && <p className="note">"Last traded price" changes only when someone trades{d.account_id === "DRYRUN_ACCOUNT" ? "; EURINR and GBPINR futures trade a few hundred lots a day, so it can stay the same for many minutes" : ""}. "Buyers / sellers now" are the best live offers: selling now would get the buyers' price. "Profit now" is the price move only. "If sold now" also takes off brokerage, taxes and the buy–sell price gap, the same way the paper trader books a sale.
          The account changes only when a trade is sold.</p>}
      </Panel>

      <Panel title="Closed trades">
        {!d.trades.length ? <div className="none">No closed trades yet.</div> : (
          <div className="table-wrap"><table className="trades-table">
            <thead><tr><th>Stock</th><th>Bought</th><th>Sold</th><th>Qty</th><th>Buy</th><th>Sell</th><th>Fees</th><th>Profit after fees</th><th>Why it ended</th></tr></thead>
            <tbody>{tradePaged.rows.map((t, i) => <tr key={i}><td>{t.symbol}</td><td>{when(t.entry_dt)}</td><td>{when(t.exit_dt)}</td><td>{t.qty}</td><td>{t.entry_price?.toFixed(2)}</td>
              <td>{t.exit_price?.toFixed(2)}</td><td>{inr(t.total_friction ?? 0)}</td><td className={tone(t.net_pnl ?? 0)}>{signed(t.net_pnl ?? 0)}</td><td>{(t.exit_reason ?? "").replace(/_/g, " ")}</td></tr>)}</tbody>
          </table><Pager paged={tradePaged} /></div>)}
      </Panel>

      <Panel title="Account value by day">{d.equity.length ? <div ref={box} className="tv-box" /> : <div className="none">The first point appears after today's close.</div>}</Panel>

      {d.account_id === "DRYRUN_ACCOUNT" && <StrategyStatus />}
    </div>
  );
}
