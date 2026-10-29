import { useEffect, useRef, useState } from "react";
import BacktestForm from "../components/backtest/BacktestForm.jsx";
import RunStatus from "../components/backtest/RunStatus.jsx";
import ReplayView from "../components/replay/ReplayView.jsx";
import { getRun, listRuns, listStrategies, startRun } from "../api/client.js";
import { go } from "../hooks/useHashRoute.js";
import Pane from "../components/ui/Pane.jsx";

export default function NewBacktestPage() {
  const [strategies, setStrategies] = useState(null);
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");
  const [recent, setRecent] = useState([]);
  const timer = useRef(null);

  useEffect(() => {
    listStrategies().then(setStrategies).catch((e) => setError(e.message));
    // A backtest already running (started earlier, or from another tab): show it live instead of a blocked form.
    listRuns().then((runs) => { setRecent(runs.filter((x) => x.status === "done").slice(0, 6)); const r = runs.find((x) => x.status === "running" || x.status === "queued"); if (r) { setRun(r); poll(r.id); } }).catch(() => {});
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
    <div className="builder">
      <div className="builder-main">
        <div className="research-head"><div className="rh-t"><h1>Backtest builder</h1><span className="ah-s">Pick a strategy, set the money and dates, and replay the result day by day with real costs.</span></div></div>
        <div className="wrap">
          <RunStatus run={run} error={error} />
          {busy && run.result?.days?.length > 0 && <ReplayView live result={run.result} title={`${run.strategy} on ${run.market}`} blurb="Live: trades, candles and account value appear as the backtest runs." />}
          {!strategies && !error && <div className="panel none">Loading strategies...</div>}
          {strategies && <BacktestForm strategies={strategies} onSubmit={submit} busy={busy} />}
        </div>
      </div>
      <aside className="builder-side">
        <Pane title="Recent results" meta="click to open">
          {!recent.length ? <p className="none">Your finished backtests will be listed here.</p> : recent.map((r) => {
            const ret = r.metrics?.return_pct;
            return <a key={r.id} href={`#/results/${r.id}`} className="run-item"><span className="ri-t">{r.label || `${r.strategy} on ${r.market}`}<small>{r.params?.start} → {r.params?.end}</small></span>
              {ret != null && <b className={ret >= 0 ? "up" : "down"}>{ret >= 0 ? "+" : ""}{Math.round(ret)}%</b>}</a>;
          })}
        </Pane>
        <Pane title="How these backtests work">
          <ul className="howlist">
            <li><b>Real costs, always.</b> Brokerage, taxes, exchange fees and slippage are charged on every trade and can't be switched off.</li>
            <li><b>Honest fills.</b> A stop that the price jumps past is filled at the next price, not at the stop.</li>
            <li><b>Same code as paper trading.</b> Each strategy's backtest runs the rules its live paper engine uses.</li>
            <li><b>One run at a time.</b> A running backtest shows live here; the result opens in Research when it finishes.</li>
          </ul>
        </Pane>
      </aside>
    </div>
  );
}
