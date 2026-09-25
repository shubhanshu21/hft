# Upstox live-trading readiness (researched 2026-09-25)

Live trading is blocked by `engine/safety_gate.py` and nothing here is needed for paper trading. This records what Upstox's own documentation, staff and
developer community say about placing REAL orders, so nobody has to rediscover it. Every claim has a source; "unverified" means only a real order settles it.

## 1. Blockers that exist today

| # | Finding | Source (date) | Effect on this project |
|---|---|---|---|
| 1 | **MCX order placement through the API is not live.** Upstox: "MCX trading is currently not live for Algos ... in the middle of the formal approval process with the MCX exchange". Users still reported it disabled in June and August 2026. The API returns "MCX orders via API are temporarily disabled". | [community thread](https://community.upstox.com/t/mcx-orders-disabled-through-api-need-their-activation-immediately/15273) (staff reply 2026-04-20), [second thread](https://community.upstox.com/t/can-we-place-limit-orders-on-mcx-segment-stocks-using-api-or-its-still-blocked/15574) (May-Aug 2026), error text on the [Place Order V3 page](https://upstox.com/developer/api-documentation/v3/place-order/) | CRUDEOILM and SILVERMIC cannot be traded live through the API at present, whatever the quantity units. Only equity and currency could. Re-check before any live plan. |
| 2 | **Static IP is mandatory for order APIs** since 2026-04-01: "any order requests from unregistered IPs will be rejected" (primary + secondary IP per user). Applies to Place / Modify / Cancel / Multi order, GTT, Exit-all; read APIs (positions, funds, candles) are exempt. | [Upstox announcement](https://upstox.com/developer/api-documentation/announcements/algo-trading-circular/) | This server's address (161.118.190.136) must be registered in the Upstox app and must be a stable/reserved address. The dashboard, watchdog and data jobs only use exempt read APIs. |
| 3 | **Algo registration** only if an algo sends more than 10 orders per second; then the exchange Algo ID / `X-Algo-Name` header is needed. | same announcement | Not applicable (this bot places a few orders a day). |
| 4 | **Market orders**: Upstox announced on 2025-09-05 that market orders would not be processed from 2025-10-01 (exchange algo rules). Since 2026-03-11 the optional `market_protection` parameter exists for Market / SL-Market orders, i.e. market orders are auto-bounded by a protection band. Status of plain market orders is unclear from the threads. | [announcement thread](https://community.upstox.com/t/regulatory-update-pausing-market-orders-via-apis/11306), [changelog](https://upstox.com/developer/api-documentation/announcements/) | `UpstoxBroker._place_order` sends MARKET without `market_protection`. Before live: send LIMIT at LTP +/- a band, or MARKET with `market_protection`, and test which is accepted. |

## 2. Order quantity units (the question that started this)

| Segment | What the evidence says | Confidence |
|---|---|---|
| **Commodity (MCX)** | Official (v2 and v3): "For commodity - number of **lots** is accepted." In June 2024 the API briefly required lot *size* (GOLDM worked only with 100); staff: "a minor issue on our end, which has since been resolved" ([thread](https://community.upstox.com/t/problem-placing-mcx-orders/6058), fixed 2024-07-03). Implemented as lots in `engine/order_units.py`. | Good, but moot until blocker 1 clears. |
| **Equity, NSE F&O** | Official: "number of **units**" in multiples of the lot size. | Good. |
| **Currency (NCD / CDS)** | Not named in the docs, so by the letter it is "other Futures & Options" = **units** (USDINR 4 lots = 4000). Staff, 2024-01-08: "For currency, we take lot size as inputs. For futures, etc, tick size is used to assess the quantity entered" ([thread](https://community.upstox.com/t/inconsistency-in-usage-of-quantity-while-placing-order/3695)): both are multiple-of-lot checks, which also reads as units, but the wording is ambiguous and staff promised a documentation disclaimer that was never added. Default `units`; `LIVE_CURRENCY_QTY_MODE=lots` flips it. | **Unverified.** Settle with one real 1-lot order. |

Other calculators use other units, and they are not the order unit: **margin** (`post_margin`) counts **lots** (measured: qty 1/2/5 = 1x/2x/5x one lot); the **brokerage** calculator counts **units** (measured: crude qty 10 = one lot's turnover).

## 3. Position quantity (used by reconciliation)

Official field text: `quantity` = "Quantity left after nullifying Day and CF buy quantity towards Day and CF sell quantity"; `multiplier` = "The quantity/lot size multiplier used for calculating P&Ls".
The official sample response shows a **USDINR position with `quantity: 1`, `multiplier: 1000.0`** and an MCX GOLDPETAL position with `quantity: 1`, `multiplier: 1.0`
([Get Positions](https://upstox.com/developer/api-documentation/get-positions/)). Reading: positions are reported in **lots** with a multiplier to convert to units, which is what
`position_reconciliation` assumes (it compares lots). Strong for currency; for MCX the only sample has a lot of 1, so it does not discriminate. The open-source OpenAlgo bridge passes the
raw value through with no conversion, which neither confirms nor refutes. **Unverified for crude/silver; check with a real position.**

## 4. Also worth knowing

- 2026-04-10: a **Kill Switch API** lets an app enable/disable trading segments programmatically. A candidate extra safety layer; not used yet.
- 2026-08-01: F&O closing moved to 15:40 (closing auction session); exchange hours are read from Upstox at runtime (`core/sessions.py`), so this needs no change.

## 5. Checklist before arming live trading

1. MCX API trading enabled by Upstox (blocker 1), or restrict live to equity/currency.
2. Static IP registered and stable (blocker 2).
3. Decide LIMIT vs MARKET-with-`market_protection` and test it (blocker 4).
4. One real 1-lot order per segment to settle currency order units and position units (sections 2-3), then set `LIVE_CURRENCY_QTY_MODE` accordingly.
5. Then, and only then, the three independent gates in `engine/safety_gate.py`.
