
const TOKEN_KEY = "hft-admin-token";

/** The admin token is only needed for destructive calls (delete a run, reset a paper account). */
function adminToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

async function request(path, options = {}, { admin = false } = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const opts = { ...options };
    const token = admin ? adminToken() : "";
    if (token) opts.headers = { ...(opts.headers || {}), "X-API-Token": token };
    const res = await fetch(path, opts);
    const body = await res.json().catch(() => ({}));
    if (res.ok) return body;
    // 401 = the server has a token configured and ours is missing/wrong: ask once, remember it, retry.
    if (admin && res.status === 401 && attempt === 0) {
      const typed = window.prompt("Admin token (BACKTEST_ADMIN_TOKEN in backend/.env):", "");
      if (typed) {
        try { localStorage.setItem(TOKEN_KEY, typed.trim()); } catch { /* private mode: retry with the typed value only */ }
        continue;
      }
    }
    throw new Error(body.error ?? `Request failed (${res.status})`);
  }
}

export const listStrategies = () => request("/api/strategies");
export const listRuns = () => request("/api/backtests");
export const getRun = (id) => request(`/api/backtests/${id}`);
export const renameRun = (id, label) => request(`/api/backtests/${id}/label`, { method: "POST", body: JSON.stringify({ label }) });
export const getPaper = (account) => request(`/api/paper${account ? `?account=${encodeURIComponent(account)}` : ""}`);
export const resetPaper = (account, confirm) => request("/api/paper/reset", { method: "POST", body: JSON.stringify({ account, confirm }) }, { admin: true });
export const deleteRun = (id) => request(`/api/backtests/${id}`, { method: "DELETE" }, { admin: true });
export const clearRuns = () => request("/api/backtests", { method: "DELETE" }, { admin: true });
export const startRun = (params) => request("/api/backtests", { method: "POST", body: JSON.stringify(params) });

/** Finished example runs shipped with the site (public/data). Same result shape as an API run. */
export async function listSamples() {
  const res = await fetch("data/index.json");
  return res.ok ? res.json() : [];
}
export const getSample = (file) => fetch(`data/${file}`).then((r) => r.json());
