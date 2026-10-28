import AppHeader from "./components/layout/AppHeader.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";
import NewBacktestPage from "./pages/NewBacktestPage.jsx";
import PaperPage from "./pages/PaperPage.jsx";
import TodayPage from "./pages/TodayPage.jsx";
import { useHashRoute } from "./hooks/useHashRoute.js";

export default function App() {
  const [page = "today", id] = useHashRoute();
  return (
    <div className="shell">
      <AppHeader page={page} />
      <main className="page">
        {page === "today" ? <TodayPage /> : page === "new" ? <NewBacktestPage /> : page === "paper" ? <PaperPage accountId={id} /> : <ResultsPage runId={id ? decodeURIComponent(id) : undefined} />}
      </main>
    </div>
  );
}
