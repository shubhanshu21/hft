import { useEffect, useMemo, useRef, useState } from "react";
import Icon from "../ui/Icon.jsx";
import { listRuns } from "../../api/client.js";
import { useOverview } from "../../hooks/useOverview.js";
import { filterItems, paletteItems } from "../../lib/palette.js";

/** Ctrl+K (or "/") opens a search over every page, paper account and saved backtest; arrows + Enter jump there, Esc closes. */
export default function CommandPalette({ open, setOpen }) {
  const { data } = useOverview();
  const [runs, setRuns] = useState([]);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef(null);
  useEffect(() => {
    const key = (e) => {
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !/input|textarea|select/i.test(e.target.tagName))) { e.preventDefault(); setOpen(true); }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [setOpen]);
  useEffect(() => { if (open) { setQ(""); setSel(0); listRuns().then((r) => setRuns(r.filter((x) => x.status === "done"))).catch(() => {}); setTimeout(() => input.current?.focus(), 0); } }, [open]);
  const items = useMemo(() => filterItems(paletteItems(data?.accounts ?? [], runs), q), [data, runs, q]);
  if (!open) return null;
  const go = (it) => { if (it) { window.location.hash = it.href; setOpen(false); } };
  return (
    <div className="pal-back" onClick={() => setOpen(false)}>
      <div className="pal" role="dialog" aria-label="Search" onClick={(e) => e.stopPropagation()}>
        <div className="pal-in"><Icon name="search" size={18} />
          <input ref={input} value={q} placeholder="Jump to a page, account or saved backtest…" aria-label="Search"
            onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, items.length - 1)); }
              if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); } if (e.key === "Enter") go(items[sel]); }} />
          <kbd>Esc</kbd></div>
        <ul className="pal-list">
          {items.length ? items.map((it, i) => (
            <li key={it.href} className={i === sel ? "on" : ""} onMouseEnter={() => setSel(i)} onClick={() => go(it)}>
              <span>{it.label}<small>{it.hint}</small></span><em>{it.group}</em></li>
          )) : <li className="none">Nothing matches "{q}".</li>}
        </ul>
      </div>
    </div>
  );
}
