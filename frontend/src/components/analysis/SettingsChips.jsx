/** The settings a run used, so a result is never read without them. */
export default function SettingsChips({ params }) {
  if (!params) return null;
  const items = params.market === "commodity" ? [                   // trend_carry: monthly ranking, exposure instead of risk per trade, MCX cost model
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Exposure", `${params.leverage}x the account`],
    ["Commodities", `${params.symbols?.length}: ${(params.symbols ?? []).join(", ")}`],
    ["Rules", "rank(12-month trend) + rank(carry), best 3, whole lots rounded to nearest"],
    ["Rebalance", "every 2 weeks (10 trading days)"],
    ["Costs", "MCX fees + half-spread"],
  ] : params.market === "crypto" && params.strategy === "dip_reversal" ? [
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Perps", `${params.symbols?.length}: ${(params.symbols ?? []).join(", ")}`],
    ["Timeframe", "5-minute candles"],
    ["Rules", "Bollinger-band dip reversal (BinHV45 + ClucMay2018), 15% target / 7% stop, up to 5 open together"],
    ["Risk per trade", params.risk_pct != null ? `${params.risk_pct}%` : "strategy default"],
    ["Costs", "taker+GST+slippage on entries/stops, maker+GST on the target, real funding"],
  ] : params.strategy === "results_gap" ? [
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Size", `${params.leverage}x the account each morning`],
    ["Rules", "results-day gap 7%+ vs market, still rising at 09:30 -> buy; sell 15:20"],
    ["Stocks", "own list of 414 liquid NSE stocks"],
    ["Costs", "Upstox intraday fees + 0.1% slippage each way"],
  ] : params.market === "crypto" ? [
    ["Start money", `₹${Number(params.capital).toLocaleString("en-IN")}`],
    ["Coins", `${params.symbols?.length}: ${(params.symbols ?? []).join(", ")}`],
    ["Strategy", params.strategy],
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
