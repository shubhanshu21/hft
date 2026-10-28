/** Which result the Results page opens on, and how the run list is grouped (2026-10-29: it used to open on an example of a strategy
 *  that was removed weeks earlier). Runs arrive newest first from the API; `live` = the strategy is paper traded now. */
export function defaultRunKey(runId, runs, samples) {
  if (runId) return runId;
  const live = runs.find((r) => r.live);
  if (live) return live.id;
  if (runs[0]) return runs[0].id;
  return samples[0] ? `sample:${samples[0].file}` : "";
}

export function groupRuns(runs, samples) {
  return [
    { label: "Live strategies", items: runs.filter((r) => r.live).map((r) => ({ key: r.id, run: r })) },
    { label: "Other saved runs", items: runs.filter((r) => !r.live).map((r) => ({ key: r.id, run: r })) },
    { label: "Old examples", items: samples.map((s) => ({ key: `sample:${s.file}`, sample: s })) },
  ].filter((g) => g.items.length);
}
