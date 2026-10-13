import { useEffect, useState } from "react";

const read = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

/** Tiny hash router: "#/results/abc" -> ["results", "abc"]. */
export function useHashRoute() {
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const on = () => setParts(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parts;
}

export const go = (path) => { window.location.hash = `#/${path}`; };
