const TABS = [["today", "Today"], ["paper", "Paper trading"], ["results", "Results"], ["new", "New backtest"]];

export default function AppHeader({ page }) {
  return (
    <header className="app-header">
      <a className="brand" href="#/today"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>HFT Lab<small>paper trading & backtests</small></span></a>
      <nav className="nav" aria-label="Pages">
        {TABS.map(([key, label]) => (
          <a key={key} href={`#/${key}`} className={page === key ? "active" : ""} aria-current={page === key ? "page" : undefined}>{label}</a>
        ))}
      </nav>
    </header>
  );
}
