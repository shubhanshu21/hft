import { useEffect, useState } from "react";

const NAMES = ["bg", "panel", "line", "fg", "muted", "accent", "gain", "loss", "grid"];
const read = () => {
  const cs = getComputedStyle(document.documentElement);
  return Object.fromEntries(NAMES.map((n) => [n, cs.getPropertyValue(`--${n}`).trim()]));
};

/** The page's colour tokens as plain strings (the chart library cannot read CSS variables); refreshed when the theme changes. */
export function useTheme() {
  const [colors, setColors] = useState(read);
  useEffect(() => {
    const update = () => setColors(read());
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", update);
    const mo = new MutationObserver(update);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => { mq.removeEventListener("change", update); mo.disconnect(); };
  }, []);
  return colors;
}

export const withAlpha = (hex, a) => {
  const h = hex.replace("#", "");
  const n = h.length === 3 ? [...h].map((c) => c + c) : h.match(/../g);
  const [r, g, b] = n.map((x) => parseInt(x, 16));
  return `rgba(${r},${g},${b},${a})`;
};
