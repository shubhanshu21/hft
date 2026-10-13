const TOKEN_KEY = "hft.backtestToken";

export const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; } };
export const setToken = (t) => { try { localStorage.setItem(TOKEN_KEY, t); } catch { /* storage blocked: the token is just not remembered */ } };

async function request(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error ?? `Request failed (${res.status})`);
  return body;
}

export const listStrategies = () => request("/api/strategies");
export const listRuns = () => request("/api/backtests");
export const getRun = (id) => request(`/api/backtests/${id}`);
export const startRun = (params, token) =>
  request("/api/backtests", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(params) });

/** Finished example runs shipped with the site (public/data). Same result shape as an API run. */
export async function listSamples() {
  const res = await fetch("data/index.json");
  return res.ok ? res.json() : [];
}
export const getSample = (file) => fetch(`data/${file}`).then((r) => r.json());
