import Panel from "../ui/Panel.jsx";

export default function RunStatus({ run, error }) {
  if (!run && !error) return null;
  return (
    <Panel title="Backtest">
      {error && <div className="error" role="alert">{error}</div>}
      {run && !error && (
        <>
          <div className="note">{run.status === "queued" ? "Queued" : "Running"}: {run.strategy} on {run.market}</div>
          <div className="progress" role="progressbar" aria-valuenow={run.progress ?? 0} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${run.progress ?? 0}%` }} /></div>
        </>
      )}
    </Panel>
  );
}
