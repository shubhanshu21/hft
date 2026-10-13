import { useEffect, useRef, useState } from "react";
import BacktestForm from "../components/backtest/BacktestForm.jsx";
import RunStatus from "../components/backtest/RunStatus.jsx";
import { getRun, listStrategies, startRun } from "../api/client.js";
import { go } from "../hooks/useHashRoute.js";

export default function NewBacktestPage() {
  const [strategies, setStrategies] = useState(null);
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");
  const timer = useRef(null);

  useEffect(() => {
    listStrategies().then(setStrategies).catch((e) => setError(e.message));
    return () => clearInterval(timer.current);
  }, []);

  const poll = (id) => {
    clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const r = await getRun(id);
        setRun(r);
        if (r.status === "done") { clearInterval(timer.current); go(`results/${id}`); }
        if (r.status === "failed") { clearInterval(timer.current); setError(r.error ?? "The backtest failed"); }
      } catch (e) { clearInterval(timer.current); setError(e.message); }
    }, 1000);
  };

  const submit = async (params, token) => {
    setError(""); setRun({ status: "queued", strategy: params.strategy, market: params.market, progress: 0 });
    try { const { id } = await startRun(params, token); poll(id); } catch (e) { setRun(null); setError(e.message); }
  };

  const busy = run && (run.status === "queued" || run.status === "running");
  return (
    <div className="wrap">
      <div className="page-head"><div><h1>New backtest</h1><div className="sub">Pick a market and a strategy, set the money, fees and slippage, and watch the result replay day by day.</div></div></div>
      <RunStatus run={run} error={error} />
      {!strategies && !error && <div className="panel none">Loading strategies...</div>}
      {strategies && <BacktestForm strategies={strategies} onSubmit={submit} busy={busy} />}
    </div>
  );
}
