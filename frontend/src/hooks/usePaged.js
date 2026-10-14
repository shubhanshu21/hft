import { useEffect, useState } from "react";

/** One page of `items` (pageSize per page). Keeps the page in range when the list grows or shrinks. */
export function usePaged(items, pageSize = 10) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  useEffect(() => { if (page > pages - 1) setPage(pages - 1); }, [page, pages]);
  const p = Math.min(page, pages - 1);
  return { rows: items.slice(p * pageSize, (p + 1) * pageSize), page: p, pages, setPage, from: items.length ? p * pageSize + 1 : 0,
           to: Math.min(items.length, (p + 1) * pageSize), total: items.length };
}
