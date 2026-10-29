import { useEffect, useRef, useState } from "react";

/** Animates a number from its last value to `target` (ease-out, ~1.1 s); instant when the viewer prefers reduced motion. */
export function useCountUp(target, ms = 1100) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    if (target == null || !Number.isFinite(target)) return undefined;
    const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { setShown(target); from.current = target; return undefined; }
    const start = from.current, t0 = performance.now();
    let raf;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 4);
      setShown(start + (target - start) * e);
      if (k < 1) raf = requestAnimationFrame(step); else from.current = target;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return shown;
}
