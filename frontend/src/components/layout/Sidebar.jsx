import Icon from "../ui/Icon.jsx";
import { useOverview } from "../../hooks/useOverview.js";
import { ACCOUNT_COLOR, statusTone } from "../../lib/overview.js";
import { inr, signed } from "../../lib/format.js";

const NAV = [["today", "Desk", "grid"], ["paper", "Accounts", "wallet"], ["results", "Research", "chart"], ["new", "Backtest", "play"]];

/** Left rail (Look B layout, 2026-10-29): the pages, then every paper account with its live value and engine light. On a phone the
 *  same four pages become a bottom tab bar and the account list is hidden (the Desk shows it). */
export default function Sidebar({ page, account, onSearch, collapsed = false, onToggle }) {
  const { data } = useOverview();
  return (
    <aside className="rail">
      <div className="rail-top">
        <a className="rail-brand" href="#/today" title="HFT Lab"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span className="rl">HFT Lab<small>paper desk</small></span></a>
        <button type="button" className="rail-fold" onClick={onToggle} aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"} aria-expanded={!collapsed}
          title={collapsed ? "Expand sidebar  [" : "Collapse sidebar  ["}><Icon name="chevron" size={16} /></button>
      </div>
      <button type="button" className="rail-search" onClick={onSearch} title="Search (Ctrl K)"><Icon name="search" size={16} /><span className="rl">Search</span><kbd className="rl">Ctrl K</kbd></button>
      <nav className="rail-nav" aria-label="Pages">
        {NAV.map(([key, label, icon]) => (
          <a key={key} href={`#/${key}`} className={page === key ? "on" : ""} aria-current={page === key ? "page" : undefined} title={label}><Icon name={icon} size={18} /><span className="rl">{label}</span></a>
        ))}
      </nav>
      {data?.accounts && (
        <div className="rail-accts">
          <div className="rail-h rl">Paper accounts</div>
          {data.accounts.map((a) => (
            <a key={a.id} href={`#/paper/${a.id}`} className={page === "paper" && account === a.id ? "on" : ""} title={`${a.label}: ${a.error ? "unavailable" : inr(a.value)}`}>
              <i className="sw" style={{ background: ACCOUNT_COLOR[a.id] }} />
              <span className="nm rl">{a.label}<small className={a.error ? "down" : a.today_pnl > 0 ? "up" : a.today_pnl < 0 ? "down" : ""}>{a.error ? "unavailable" : `${inr(a.value)} · today ${a.today_pnl ? signed(a.today_pnl) : "₹0"}`}</small></span>
              <i className={`led ${statusTone(a.engine)}`} title={a.engine?.text} />
            </a>
          ))}
        </div>
      )}
      <div className="rail-foot rl">Paper money only. No real orders are placed.</div>
    </aside>
  );
}
