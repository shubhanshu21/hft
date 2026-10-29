/** A desk panel with a terminal-style header bar: title on the left, a short note or actions on the right (2026-10-29 layout). */
export default function Pane({ title, meta, actions, className = "", children, id }) {
  return (
    <section className={`pane ${className}`} id={id}>
      <header className="pane-h"><h2>{title}</h2>{meta && <span className="pane-m">{meta}</span>}{actions && <span className="pane-a">{actions}</span>}</header>
      <div className="pane-b">{children}</div>
    </section>
  );
}
