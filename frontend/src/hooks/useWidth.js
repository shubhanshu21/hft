import { useEffect, useRef, useState } from "react";

/** Width in pixels of the element the returned ref is attached to; charts use it to draw at real size instead of scaling text. */
export function useWidth(initial = 900) {
  const ref = useRef(null);
  const [width, setWidth] = useState(initial);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}
