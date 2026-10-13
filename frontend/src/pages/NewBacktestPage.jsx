import { useEffect, useRef, useState } from "react";
import BacktestForm from "../components/backtest/BacktestForm.jsx";
import RunStatus from "../components/backtest/RunStatus.jsx";
import ReplayView from "../components/replay/ReplayView.jsx";
import { getRun, listRuns, listStrategies, startRun } from "../api/client.js";
import { go } from "../hooks/useHashRoute.js";

export default function NewBacktestPage() {
  const [strategies, setStrategies] = useState(null);
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");
  const timer = useRef(null);

  useEffect(() => {
    listStrategies().then(setStrategies).catch((e) => setError(e.message));
    // A backtest already running (started earlier, or from another tab): show it live instead of a blocked form.
    listRuns().then((runs) => { const r = runs.find((x) => x.status === "running" || x.status === "queued"); if (r) { setRun(r); poll(r.id); } }).catch(() => {});
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
    }, 700);
  };

  const submit = async (params) => {
    setError(""); setRun({ status: "queued", strategy: params.strategy, market: params.market, progress: 0 });
    try { const { id } = await startRun(params); poll(id); } catch (e) { setRun(null); setError(e.message); }
  };

  const busy = run && (run.status === "queued" || run.status === "running");
  return (
    <div className="wrap">
      <div className="page-head"><div><h1>New backtest</h1><div className="sub">Pick a market and a strategy, set the money, fees and slippage, and watch the result replay day by day.</div></div></div>
      <RunStatus run={run} error={error} />
      {busy && run.result?.days?.length > 0 && <ReplayView live result={run.result} title={`${run.strategy} on ${run.market}`} blurb="Live: trades, candles and account value appear as the backtest runs." />}
      {!strategies && !error && <div className="panel none">Loading strategies...</div>}
      {strategies && <BacktestForm strategies={strategies} onSubmit={submit} busy={busy} />}
    </div>
  );
}
