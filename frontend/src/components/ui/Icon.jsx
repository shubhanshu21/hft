/** A small line-icon set (24px grid, 1.75 stroke, currentColor) -- drawn here so the site needs no icon library. */
const P = {
  wallet: <><path d="M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3" /><path d="M21 9h-6a3 3 0 0 0 0 6h6z" /><circle cx="15.5" cy="12" r=".6" /></>,
  percent: <><path d="M19 5 5 19" /><circle cx="6.5" cy="6.5" r="2.5" /><circle cx="17.5" cy="17.5" r="2.5" /></>,
  calendar: <><rect x="3" y="4.5" width="18" height="16" rx="2.5" /><path d="M3 9.5h18M8 2.5v4M16 2.5v4" /></>,
  down: <><path d="M3 7l6 6 4-4 8 8" /><path d="M21 11v6h-6" /></>,
  up: <><path d="M3 17l6-6 4 4 8-8" /><path d="M21 13V7h-6" /></>,
  trophy: <><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0z" /><path d="M7 6H4a3 3 0 0 0 3 4M17 6h3a3 3 0 0 1-3 4" /></>,
  layers: <><path d="m12 3 9 5-9 5-9-5z" /><path d="m3 13 9 5 9-5" /></>,
  coins: <><ellipse cx="9" cy="7" rx="6" ry="3" /><path d="M3 7v5c0 1.7 2.7 3 6 3s6-1.3 6-3V7" /><path d="M15 11.5c3.3 0 6 1.3 6 3s-2.7 3-6 3-6-1.3-6-3" /><path d="M21 14.5v4c0 1.7-2.7 3-6 3s-6-1.3-6-3v-1.6" /></>,
  flame: <path d="M12 21c4 0 7-2.8 7-6.7 0-3.8-3-6-4-9.3-1.3 2.3-2.2 3.4-3.5 4.3C10.6 7.6 9.7 5 9.5 3 6.6 5.6 5 9.4 5 14.3 5 18.2 8 21 12 21z" />,
  chart: <><path d="M3 3v18h18" /><path d="m7 15 4-5 3 3 5-6" /></>,
  receipt: <><path d="M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z" /><path d="M9 8h6M9 12h6" /></>,
  play: <path d="M7 4.5v15l12.5-7.5z" />,
  sparkle: <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />,
  grid: <><rect x="3" y="3" width="7.5" height="9" rx="1.5" /><rect x="13.5" y="3" width="7.5" height="5" rx="1.5" /><rect x="13.5" y="11" width="7.5" height="10" rx="1.5" /><rect x="3" y="15" width="7.5" height="6" rx="1.5" /></>,
  chevron: <path d="m14.5 6-6 6 6 6" />,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  inbox: <><path d="M3 13h5l1.5 3h5L16 13h5" /><path d="M5.5 5h13L21 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6z" /></>,
};
export default function Icon({ name, size = 18, className = "" }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ width: size, height: size }}>{P[name]}</svg>
  );
}
