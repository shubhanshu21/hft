/** Previous / next buttons for a usePaged() list. Hidden when everything fits on one page. */
export default function Pager({ paged }) {
  const { page, pages, setPage, from, to, total } = paged;
  if (pages <= 1) return null;
  return (
    <div className="pager">
      <button type="button" onClick={() => setPage(0)} disabled={page === 0} aria-label="First page">«</button>
      <button type="button" onClick={() => setPage(page - 1)} disabled={page === 0}>‹ Previous</button>
      <span>{from}–{to} of {total}</span>
      <button type="button" onClick={() => setPage(page + 1)} disabled={page >= pages - 1}>Next ›</button>
      <button type="button" onClick={() => setPage(pages - 1)} disabled={page >= pages - 1} aria-label="Last page">»</button>
    </div>
  );
}
