
async function request(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `Request failed (${res.status})`);
  return body;
}

export const listStrategies = () => request("/api/strategies");
export const listRuns = () => request("/api/backtests");
export const getRun = (id) => request(`/api/backtests/${id}`);
export const startRun = (params) => request("/api/backtests", { method: "POST", body: JSON.stringify(params) });

/** Finished example runs shipped with the site (public/data). Same result shape as an API run. */
export async function listSamples() {
  const res = await fetch("data/index.json");
  return res.ok ? res.json() : [];
}
export const getSample = (file) => fetch(`data/${file}`).then((r) => r.json());
