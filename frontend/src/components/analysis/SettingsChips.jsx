/** The settings a run used, so a result is never read without them. */
export default function SettingsChips({ params }) {
  if (!params) return null;
  const items = params.market === "commodity" ? [                   // trend_carry: monthly ranking, exposure instead of risk per trade, MCX cost model
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Exposure", `${params.leverage}x the account`],
    ["Commodities", `${params.symbols?.length}: ${(params.symbols ?? []).join(", ")}`],
    ["Rebalance", "monthly"],
    ["Costs", "MCX fees + half-spread"],
  ] : params.market === "crypto" ? [                                // calm_coins: 5 coins' futures while the market is calm
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Leverage", `${params.leverage}x`],
    ["Holds", "BTC ETH SOL DOGE TRX futures (equal money) while the crypto market is calm, else cash"],
    ["Checks", "weekly, 7 weekday parts"],
    ["Tax", params.tax === false || params.tax === "none" ? "off (before tax)" : params.tax === "business" ? "business income: 31.2% of yearly net profit" : "strict: 31.2% of each winning trade"],
  ] : params.strategy === "earnings_drift" ? [                       // results reaction across the day's top-200 stocks
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Buys", "after results beat the market by 4%+ on 2x volume"],
    ["Holds", "up to 5 stocks, 40 trading days, no stop"],
    ["Slippage", `${params.slippage_bp} bp`],
  ] : params.strategy === "momentum_portfolio" ? [                   // quarterly ranking of the day's top-200 stocks
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Stocks", "15 best 6-month return / volatility of the day's 200 most-traded"],
    ["Rebalance", "quarterly, keep while in the top 30"],
    ["Slippage", `${params.slippage_bp} bp`],
  ] : params.strategy === "breakout_pair" ? [                        // picks its own stocks; fixed holdings and exits
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Risk per trade", `${params.risk_pct}%`],
    ["Stocks", "the day's 200 most-traded NSE"],
    ["Holdings", "up to 5 quiet-day + 3 55-day breakouts"],
    ["Slippage", `${params.slippage_bp} bp`],
    ["Exits", "2.5-ATR stop, 3.25-ATR trail, 20-day dead money"],
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
