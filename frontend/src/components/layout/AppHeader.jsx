const TABS = [["results", "Results"], ["compare", "Compare"], ["paper", "Paper trading"], ["new", "New backtest"]];

export default function AppHeader({ page }) {
  return (
    <header className="app-header">
      <span className="brand">HFT Backtest Lab</span>
      <nav className="nav" aria-label="Pages">
        {TABS.map(([key, label]) => (
          <a key={key} href={`#/${key}`} className={page === key ? "active" : ""} aria-current={page === key ? "page" : undefined}>{label}</a>
        ))}
      </nav>
    </header>
  );
}
