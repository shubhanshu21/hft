import AppHeader from "./components/layout/AppHeader.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";
import NewBacktestPage from "./pages/NewBacktestPage.jsx";
import { useHashRoute } from "./hooks/useHashRoute.js";

export default function App() {
  const [page = "results", id] = useHashRoute();
  return (
    <div className="wrap">
      <AppHeader page={page} />
      {page === "new" ? <NewBacktestPage /> : <ResultsPage runId={id ? decodeURIComponent(id) : undefined} />}
    </div>
  );
}
