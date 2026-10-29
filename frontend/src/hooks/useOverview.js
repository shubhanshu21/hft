import { useEffect, useState } from "react";
import { getOverview } from "../api/client.js";

// One shared /api/overview feed for the sidebar, ticker, status bar and Desk page: fetched once a minute, however many parts listen.
let data = null, error = "", timer = null, at = null;
const subs = new Set();
const emit = () => subs.forEach((f) => f({ data, error, at }));
const load = () => getOverview().then((d) => { data = d; error = ""; at = new Date(); emit(); }).catch((e) => { error = e.message; emit(); });

export function useOverview() {
  const [s, setS] = useState({ data, error, at });
  useEffect(() => {
    subs.add(setS);
    if (!timer) { load(); timer = setInterval(load, 60000); }
    return () => { subs.delete(setS); if (!subs.size) { clearInterval(timer); timer = null; } };
  }, []);
  return s;
}
