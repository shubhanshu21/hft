/** Helpers for the Today page. */
export const pctOf = (value, start) => (start ? Math.round((value / start - 1) * 1000) / 10 : 0);

export const statusTone = (engine) => (engine?.ok === true ? "good" : engine?.ok === false ? "bad" : "unknown");
