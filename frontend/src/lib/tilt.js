/** Gentle 3D tilt toward the pointer for every element marked data-tilt (2026-11-18 "3D Desk"). One delegated listener for the page;
 *  off for touch screens and reduced motion. Returns the uninstall function. */
export function tiltAngles(rect, x, y, max = 3) {
  const dx = (x - rect.left) / rect.width - 0.5, dy = (y - rect.top) / rect.height - 0.5;
  return { rx: +(-dy * 2 * max).toFixed(2), ry: +(dx * 2 * max).toFixed(2) };
}

export function installTilt(doc = document, win = window) {
  if (win.matchMedia?.("(prefers-reduced-motion: reduce)").matches || !win.matchMedia?.("(pointer: fine)").matches) return () => {};
  let el = null;
  const reset = () => { if (el) { el.style.removeProperty("--rx"); el.style.removeProperty("--ry"); el = null; } };
  const move = (e) => {
    const t = e.target.closest?.("[data-tilt]");
    if (t !== el) reset();
    if (!t) return;
    el = t;
    const { rx, ry } = tiltAngles(t.getBoundingClientRect(), e.clientX, e.clientY, Number(t.dataset.tilt) || 3);
    t.style.setProperty("--rx", `${rx}deg`); t.style.setProperty("--ry", `${ry}deg`);
  };
  doc.addEventListener("pointermove", move, { passive: true });
  doc.addEventListener("pointerleave", reset);
  return () => { doc.removeEventListener("pointermove", move); doc.removeEventListener("pointerleave", reset); reset(); };
}
