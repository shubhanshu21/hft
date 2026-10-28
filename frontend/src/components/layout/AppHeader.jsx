import Icon from "../ui/Icon.jsx";

const TABS = [["today", "Today"], ["paper", "Paper trading"], ["results", "Results"], ["new", "New backtest"]];

export default function AppHeader({ page }) {
  return (
    <header className="app-header">
      <span className="brand"><span className="brand-mark"><Icon name="chart" size={17} /></span><span>HFT Backtest Lab<small>paper trading & backtests</small></span></span>
      <nav className="nav" aria-label="Pages">
        {TABS.map(([key, label]) => (
          <a key={key} href={`#/${key}`} className={page === key ? "active" : ""} aria-current={page === key ? "page" : undefined}>{label}</a>
        ))}
      </nav>
    </header>
  );
}
