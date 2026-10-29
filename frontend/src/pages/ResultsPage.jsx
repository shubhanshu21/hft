import { useEffect, useMemo, useState } from "react";
import ReplayView from "../components/replay/ReplayView.jsx";
import Pane from "../components/ui/Pane.jsx";
import Icon from "../components/ui/Icon.jsx";
import { clearRuns, deleteRun, getRun, getSample, listRuns, listSamples, renameRun } from "../api/client.js";
import { go } from "../hooks/useHashRoute.js";
import { defaultRunKey, groupRuns } from "../lib/runs.js";
import { filterItems } from "../lib/palette.js";

const title = (r) => r.label || `${r.strategy} on ${r.market}`;
const dates = (r) => `${r.params?.start ?? ""} → ${r.params?.end ?? ""}`;

/** Research (2026-10-29 layout redesign): saved backtests listed on the left (searchable, grouped: live strategies first), the chosen
 *  result replayed on the right with its actions in the header. */
export default function ResultsPage({ runId }) {
  const [samples, setSamples] = useState([]);
  const [saved, setSaved] = useState([]);
  const [loaded, setLoaded] = useState(null);       // { title, blurb, params, result }
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(null);      // "one" | "all": the button asks once more before deleting (no browser dialogs)
  const [naming, setNaming] = useState(null);        // text while renaming
  const [q, setQ] = useState("");

  const refresh = () => listRuns().then((r) => setSaved(r.filter((x) => x.status === "done"))).catch(() => {});
  useEffect(() => { listSamples().then(setSamples).catch(() => {}); refresh(); }, [runId]);

  const key = defaultRunKey(runId, saved, samples);          // newest run of a live strategy, not an old example
  const current = saved.find((r) => r.id === key);
  const groups = useMemo(() => {
    const words = q.trim();
    if (!words) return groupRuns(saved, samples);
    const keep = new Set(filterItems(saved.map((r) => ({ id: r.id, label: title(r), hint: `${r.strategy} ${r.market} ${dates(r)}`, group: "" })), words, 500).map((x) => x.id));
    return groupRuns(saved.filter((r) => keep.has(r.id)), []);
  }, [saved, samples, q]);

  useEffect(() => {
    if (!key) return;
    setError(""); setLoaded(null); setNaming(null);
    if (key.startsWith("sample:")) {
      const s = samples.find((x) => `sample:${x.file}` === key);
      getSample(key.slice(7)).then((result) => setLoaded({ title: s?.title, blurb: s?.blurb, result })).catch(() => setError("Could not load this result"));
    } else {
      getRun(key).then((r) => (r.result
        ? setLoaded({ title: title(r), blurb: `${r.strategy} on ${r.market}, ${r.params.start} to ${r.params.end}`, params: { ...r.params, market: r.market, strategy: r.strategy }, result: r.result })
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

  const list = (
    <Pane title="Saved results" meta={`${saved.length}`} className="runs-pane" actions={<a href="#/new" className="run-new"><Icon name="play" size={13} /> New</a>}>
      <div className="runs-search"><Icon name="search" size={15} /><input type="text" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by strategy, market, date" aria-label="Filter saved results" /></div>
      {groups.map((g) => (
        <div key={g.label} className="runs-group">
          <div className="rg-h">{g.label}</div>
          {g.items.map((it) => {
            const r = it.run, ret = r?.metrics?.return_pct;
            return (
              <a key={it.key} href={`#/results/${it.key}`} className={`run-item${it.key === key ? " on" : ""}`}>
                <span className="ri-t">{r ? title(r) : `Example: ${it.sample.title}`}<small>{r ? `${r.market} · ${dates(r)}` : "shipped example"}</small></span>
                {ret != null && <b className={ret >= 0 ? "up" : "down"}>{ret >= 0 ? "+" : ""}{Math.round(ret)}%</b>}
              </a>);
          })}
        </div>
      ))}
      {!groups.length && <p className="none">{q ? `Nothing matches "${q}".` : "No results yet. Run a backtest to see one here."}</p>}
      {saved.length > 0 && (confirm === "all"
        ? <div className="runs-danger"><span className="note">Delete all {saved.length} saved results? This cannot be undone.</span><button type="button" className="danger" onClick={() => remove("all")}>Yes, clear all</button><button type="button" onClick={() => setConfirm(null)}>Cancel</button></div>
        : <button type="button" className="linkish danger-text runs-clear" onClick={() => setConfirm("all")}>Clear all saved results…</button>)}
    </Pane>
  );

  return (
    <div className="research">
      <div className="research-list">{list}</div>
      <div className="research-main">
        <div className="research-head">
          <div className="rh-t"><h1>{loaded?.title ?? "Research"}</h1>{current && <span className="ah-s">{current.strategy} · {current.market} · {dates(current)}</span>}</div>
          {current && <div className="rh-a">
            {naming == null
              ? <button type="button" onClick={() => setNaming(current.label ?? "")}>Rename</button>
              : <><input type="text" maxLength={60} value={naming} onChange={(e) => setNaming(e.target.value)} placeholder="e.g. 3x, last 12 months" aria-label="Name" /><button type="button" className="main" onClick={saveName}>Save name</button><button type="button" onClick={() => setNaming(null)}>Cancel</button></>}
            {confirm === "one"
              ? <><span className="note">Delete this result?</span><button type="button" className="danger" onClick={() => remove("one")}>Yes, delete</button><button type="button" onClick={() => setConfirm(null)}>Cancel</button></>
              : <button type="button" onClick={() => setConfirm("one")}>Delete</button>}
          </div>}
        </div>
        {error && <div className="panel error" role="alert">{error}</div>}
        {!loaded && !error && <div className="panel none">{key ? "Loading..." : "No results yet. Run a backtest to see one here."}</div>}
        {loaded && <div className="wrap"><ReplayView key={key} result={loaded.result} title={loaded.title} blurb={loaded.blurb} params={loaded.params} fileName={`trades_${key.replace("sample:", "")}.csv`} /></div>}
      </div>
    </div>
  );
}
