import { useOverview } from "../../hooks/useOverview.js";
import { statusTone } from "../../lib/overview.js";

/** Thin bottom bar: engine health, when the numbers were refreshed, and the reminder that this is paper money. */
export default function StatusBar() {
  const { data, error, at } = useOverview();
  const bad = (data?.accounts ?? []).filter((a) => a.error || statusTone(a.engine) === "bad");
  return (
    <footer className="statusbar">
      <span className={bad.length || error ? "bad" : "good"}><i />{error ? "Prices unavailable" : bad.length ? `${bad.map((a) => a.label).join(", ")}: needs attention` : "All engines running"}</span>
      <span>{at ? `Updated ${at.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })} IST · every minute` : "Loading…"}</span>
      <span className="sb-right">Paper trading · no real orders · <kbd>Ctrl K</kbd> search</span>
    </footer>
  );
}
