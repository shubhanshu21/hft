import { useEffect, useState } from "react";
import { readCollapsed, saveCollapsed } from "./lib/palette.js";
import Sidebar from "./components/layout/Sidebar.jsx";
import TopBar from "./components/layout/TopBar.jsx";
import StatusBar from "./components/layout/StatusBar.jsx";
import CommandPalette from "./components/layout/CommandPalette.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";
import NewBacktestPage from "./pages/NewBacktestPage.jsx";
import PaperPage from "./pages/PaperPage.jsx";
import TodayPage from "./pages/TodayPage.jsx";
import { useHashRoute } from "./hooks/useHashRoute.js";

/** App frame (2026-10-29 layout redesign): left rail, top bar (tape + clocks), the page, a status bar; Ctrl+K search over everything. */
export default function App() {
  const [page = "today", id] = useHashRoute();
  const [searching, setSearching] = useState(false);
  const [collapsed, setCollapsed] = useState(() => readCollapsed(typeof localStorage === "undefined" ? null : localStorage));
  const toggle = () => setCollapsed((c) => { saveCollapsed(localStorage, !c); return !c; });
  useEffect(() => {                                              // "[" folds / unfolds the sidebar (not while typing in a field)
    const key = (e) => { if (e.key === "[" && !/input|textarea|select/i.test(e.target.tagName) && !e.ctrlKey && !e.metaKey) toggle(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  return (
    <div className={`app${collapsed ? " rail-min" : ""}`}>
      <Sidebar page={page} account={id} onSearch={() => setSearching(true)} collapsed={collapsed} onToggle={toggle} />
      <div className="app-main">
        <TopBar />
        <main className="page">
          {page === "today" ? <TodayPage /> : page === "new" ? <NewBacktestPage /> : page === "paper" ? <PaperPage accountId={id} /> :
            <ResultsPage runId={id ? decodeURIComponent(id) : undefined} />}
        </main>
        <StatusBar />
      </div>
      <CommandPalette open={searching} setOpen={setSearching} />
    </div>
  );
}
