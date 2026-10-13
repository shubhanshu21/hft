/** The settings a run used, so a result is never read without them. */
export default function SettingsChips({ params }) {
  if (!params) return null;
  const items = [
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Risk per trade", `${params.risk_pct}%`],
    ["Most holdings", params.max_positions],
    ["Leverage", `${params.leverage}x`],
    ["Slippage", `${params.slippage_bp} bp`],
    ["Symbols", params.symbols?.length],
    ["Extra trailing stop", params.trail_stop_pct ? `${params.trail_stop_pct}% after ${params.trail_after_pct}%` : "off"],
    ["Trailing profit", params.profit_trail_pct ? `${params.profit_trail_pct}%` : "off"],
    ["Add to winners", params.pyramid_pct == null ? "strategy default" : params.pyramid_pct ? `at +${params.pyramid_pct}%` : "never"],
  ];
  return (
    <div className="settings">
      {items.map(([k, v]) => <span key={k} className="chip static"><span className="k">{k}</span> {v}</span>)}
      {params.risk_pct > 3 && <span className="warn inline">High risk per trade: profits and losses are both magnified.</span>}
    </div>
  );
}
