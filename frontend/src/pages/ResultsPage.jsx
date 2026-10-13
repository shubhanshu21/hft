import { useEffect, useState } from "react";
import ReplayView from "../components/replay/ReplayView.jsx";
import { clearRuns, deleteRun, getRun, getSample, listRuns, listSamples, renameRun } from "../api/client.js";
import { go } from "../hooks/useHashRoute.js";

const label = (r) => `${r.label ? `${r.label}: ` : ""}${r.strategy} on ${r.market}, ${r.params?.start} to ${r.params?.end}${r.metrics ? ` (${r.metrics.return_pct >= 0 ? "+" : ""}${r.metrics.return_pct}%)` : ""}`;

export default function ResultsPage({ runId }) {
  const [samples, setSamples] = useState([]);
  const [saved, setSaved] = useState([]);
  const [loaded, setLoaded] = useState(null);       // { title, blurb, params, result }
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(null);      // "one" | "all": the button asks once more before deleting (no browser dialogs)
  const [naming, setNaming] = useState(null);        // text while renaming

  const refresh = () => listRuns().then((r) => setSaved(r.filter((x) => x.status === "done"))).catch(() => {});
  useEffect(() => { listSamples().then(setSamples).catch(() => {}); refresh(); }, [runId]);

  const key = runId ?? (samples[0] ? `sample:${samples[0].file}` : saved[0] ? saved[0].id : "");
  const current = saved.find((r) => r.id === key);

  useEffect(() => {
    if (!key) return;
    setError(""); setLoaded(null); setNaming(null);
    if (key.startsWith("sample:")) {
      const s = samples.find((x) => `sample:${x.file}` === key);
      getSample(key.slice(7)).then((result) => setLoaded({ title: s?.title, blurb: s?.blurb, result })).catch(() => setError("Could not load this result"));
    } else {
      getRun(key).then((r) => (r.result
        ? setLoaded({ title: r.label || `${r.strategy} on ${r.market}`, blurb: `${r.strategy} on ${r.market}, ${r.params.start} to ${r.params.end}`, params: r.params, result: r.result })
        : setError(r.error ?? "This run has no result"))).catch((e) => setError(e.message));
    }
  }, [key, samples.length]);

  const remove = async (what) => {
    setError(""); setConfirm(null);
    try { if (what === "all") await clearRuns(); else await deleteRun(key); await refresh(); go("results"); } catch (e) { setError(e.message); }
  };
  const saveName = async () => {
    try { await renameRun(key, naming); await refresh(); setLoaded((l) => l && { ...l, title: naming || l.blurb }); setNaming(null); } catch (e) { setError(e.message); }
  };

  return (
    <div className="wrap">
      <div className="panel run-bar">
        <label htmlFor="run" className="note">Result</label>
        <select id="run" value={key} onChange={(e) => go(`results/${e.target.value}`)}>
          {samples.map((s) => <option key={s.file} value={`sample:${s.file}`}>Example: {s.title}</option>)}
          {saved.map((r) => <option key={r.id} value={r.id}>{label(r)}</option>)}
        </select>
        <a href="#/new" className="note">Run a new backtest</a>
        {current && (naming == null
          ? <button type="button" onClick={() => setNaming(current.label ?? "")}>Rename</button>
          : <><input type="text" maxLength={60} value={naming} onChange={(e) => setNaming(e.target.value)} placeholder="e.g. 10% risk test" aria-label="Name" /><button type="button" className="main" onClick={saveName}>Save name</button><button type="button" onClick={() => setNaming(null)}>Cancel</button></>)}
        {current && (confirm === "one"
          ? <><span className="note">Delete this result?</span><button type="button" className="danger" onClick={() => remove("one")}>Yes, delete</button><button type="button" onClick={() => setConfirm(null)}>Cancel</button></>
          : <button type="button" onClick={() => setConfirm("one")}>Delete this result</button>)}
        {saved.length > 0 && (confirm === "all"
          ? <><span className="note">Delete all {saved.length} saved results?</span><button type="button" className="danger" onClick={() => remove("all")}>Yes, clear all</button><button type="button" onClick={() => setConfirm(null)}>Cancel</button></>
          : <button type="button" onClick={() => setConfirm("all")}>Clear all results</button>)}
      </div>
      {error && <div className="panel error" role="alert">{error}</div>}
      {!loaded && !error && <div className="panel none">{key ? "Loading..." : "No results yet. Run a backtest to see one here."}</div>}
      {loaded && <ReplayView key={key} result={loaded.result} title={loaded.title} blurb={loaded.blurb} params={loaded.params} fileName={`trades_${key.replace("sample:", "")}.csv`} />}
    </div>
  );
}
