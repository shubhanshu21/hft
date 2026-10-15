/** The settings a run used, so a result is never read without them. */
export default function SettingsChips({ params }) {
  if (!params) return null;
  const items = params.market === "commodity" ? [                   // trend_carry: monthly ranking, exposure instead of risk per trade, MCX cost model
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Exposure", `${params.leverage}x the account`],
    ["Commodities", `${params.symbols?.length}: ${(params.symbols ?? []).join(", ")}`],
    ["Rebalance", "monthly"],
    ["Costs", "MCX fees + half-spread"],
  ] : [
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Risk per trade", `${params.risk_pct}%`],
    ["Most holdings", params.max_positions],
    ...(params.leverage > 1 ? [["Upstox margin", `${params.leverage}x`]] : []),
    ...(params.market === "currency" ? [] : [["Slippage", `${params.slippage_bp} bp`]]),      // currency: the cost model already has the real spread
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
