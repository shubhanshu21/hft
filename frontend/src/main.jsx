import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles/index.css";

createRoot(document.getElementById("root")).render(<App />);

// Stale-tab guard (2026-10-23). This is a one-page app: its code loads once and moving between pages never reloads it, so a tab
// opened before a deploy keeps showing old labels and rules (a results page showed the removed "skewness" rule after the fix
// was live). Every minute, and whenever the tab comes back into view, check whether index.html now points at a newer bundle;
// if it does, reload on the next page change, so nothing typed on the current page is lost.
if (!import.meta.env.DEV) {
  const mine = new URL(import.meta.url).pathname.split("/").pop();
  let stale = false;
  const check = async () => {
    try {
      const html = await (await fetch("/index.html", { cache: "no-store" })).text();
      const live = (html.match(/\/assets\/(index-[^"']+\.js)/) || [])[1];
      if (live && live !== mine) stale = true;
    } catch { /* offline or server restarting: try again next time */ }
  };
  setInterval(check, 60_000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") check(); });
  window.addEventListener("hashchange", () => { if (stale) window.location.reload(); });
}
