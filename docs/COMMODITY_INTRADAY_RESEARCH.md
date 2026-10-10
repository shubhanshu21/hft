# MCX intraday fair-value gap (2026-10-10)

Idea, carried over from currency (`docs/CURRENCY_RESEARCH.md`, where thin EURINR/GBPINR lag EUR/USD x USDINR and catch up): an MCX
contract should track its world price x USDINR, and a mini contract its full-size contract; a lag that closes within 15-60 minutes would
be tradable.

**Answer: no edge.** MCX gold, silver, crude and natural gas track the world price too closely at 5 minutes.

## Data and checks

- MCX 5-minute bars 2026-05-18 to 10-09 (~100 days). The CRUDEOILM and NATGASMINI archives are copies of CRUDEOIL and NATURALGAS
  (identical prices and volumes), and there is no SILVERMIC file, so the mini-vs-full test exists only for GOLDTEN vs GOLD.
- World prices: Dukascopy 5-minute gold, silver, WTI, natural gas (`var/archive/global/`), x NSE USDINR (carried after 17:00).
- Gap = log(MCX / fair value) minus its running same-day median (resets daily, so overnight contract switches do not leak in).

## Results

First look (first 70% of days, correlation of the gap with the next 15-60 min move): gold -0.05 to -0.09, GOLDTEN vs GOLD -0.07 to
-0.09, crude -0.04 to -0.07, natural gas 0 to -0.04, silver ~0 (EURINR: -0.43).

Walk-forward test (rules fixed before running; fade-the-gap rule, ridge, LightGBM, ensemble; horizons 15/30/60 min; thresholds chosen
walk-forward; costs from `markets/commodity/costs.py`: CRUDEOILM 5 lots 5.6 bp, NATGASMINI 5 lots 7.3 bp, GOLDTEN 2 lots 6.3 bp,
SILVERMIC 2 lots 5.7 bp):

- Out-of-sample correlation of predictions with the next move: 0.00-0.08.
- Development (first 70%): 2 of 48 configurations net-positive (NATGASMINI ridge 60 min +1.3 bp a trade, t = 0.3; SILVERMIC ridge
  30 min +0.7 bp, t = 0.2); every crude and gold configuration negative.
- Holdout (last 30%, run once): both lose (NATGASMINI -1.7 bp a trade over 92 trades; SILVERMIC -0.3 bp).

## Not testable now

Commodity carry (futures curve shape) needs years of every MCX contract month; mcxindia.com refuses automated downloads (403) and
Upstox serves only current contracts. Earlier daily rules (breakout, moving averages, momentum, RSI) are in `docs/SWING_RESEARCH.md`:
only 12-month momentum showed a weak edge (paper since 2026-10-08).

## Same-commodity pairs: mini vs full, and next month vs this month (2026-10-10)

The currency recipe (`markets/currency/strategies/tf_5min/parity`: a thin contract lags the liquid prices it is built from) applied inside MCX. Real
1-minute history of 42 contracts downloaded from Upstox (`var/archive/mcx_contracts/`; Upstox serves only listed contracts, so ~2-5
months each). Gap = log(thin) - log(sibling) minus its 60-bar median; fade it on the thin leg for 30 minutes; threshold chosen on the
first 60% of days, tested on the last 40%. Costs: CTT 0.01% sell side, exchange, stamp, SEBI, GST (~1.8 bp) + Rs70.8 brokerage at
~Rs5 lakh a trade (1.4 bp) + the thin leg's Roll spread.

The minis are not the thin side: retail trades them more (GOLDPETAL 56k contracts a day vs GOLDTEN 6k; SILVERMIC 56k vs SILVERM 16k;
CRUDEOILM 37k vs CRUDEOIL 15k).

| pair (same expiry) | correlation (first 60%) | cost bp | test |
|---|---|---|---|
| CRUDEOILM vs CRUDEOIL | -0.06 | 5.3 | -4.2 bp a trade, 157 trades |
| ZINCMINI vs ZINC | ~0 | 7.3 | -4.0 bp, 204 trades |
| GOLDGUINEA vs GOLDTEN | -0.13 to -0.22 | 5.7-7.8 | -8.5 bp (Dec, 366 trades); too few in Oct/Nov |
| SILVER100 vs SILVERMIC | -0.17 to -0.19 | 9.6 | 3 trades |
| GOLDPETAL, NATGASMINI, SILVERMIC, ALUMINI, LEADMINI | -0.15 to 0 | 4.4-14 | nothing clears costs even on the choosing days |

Next month vs this month: the thinnest next months lag strongly (CRUDEOIL Dec -0.66, ALUMINI / ZINC Nov -0.71 to -0.76, NATGASMINI
Dec -0.43) but trade 90-1,600 contracts a day with 9-15 bp round trips; every setup tested lost (-1 to -8 bp a trade).

**Why MCX fails where EURINR/GBPINR works:** where the lag is big the book is so thin that the spread is bigger still; where the book
is tight, arbitrage desks leave no lag. The crosses sit in between (5-9 bp lag, 3-4 bp spread). A passive (limit-order) version on
GOLDGUINEA / SILVER100 would need their order book recorded first.

## Commodity-specific ideas (2026-10-10, rules fixed before running)

World prices: Dukascopy 5-minute gold, silver, WTI, natgas 2024-01..2026-10; Yahoo daily gold/silver 2005-2026; MCX 5-minute (~100
days). Costs at MCX sizes: CRUDEOILM 5.6 bp, NATGASMINI 7.3, GOLDTEN 6.3, SILVERMIC 5.7. Halves: 2024 vs 2025-26 (daily: 2005-15 vs
2016-26). Luck control: 1000 random directions.

| idea | first half | second half | verdict |
|---|---|---|---|
| A. Crude EIA inventory report (Wed 10:30 New York): follow the release bar for 60 min | +16.4 bp a trade (49, t 1.6) | +0.9 bp (89, t 0.1) | fails: worked in 2024, gone since |
| A. Natgas EIA storage (Thu 10:30 NY), same rule | -5.7 | -11.3 | fails |
| B. Gold/silver ratio, fade z > 2 (60-day), exit z < 0.5 or 20 days | +1.5 (53) | -41.7 (53) | fails; 1.5 / 2.5 worse |
| C. Overnight + first half hour -> last MCX half hour (4 commodities) | -4 to -11 | -2 to -10 | fails |
| D. MCX 09:00 gap to world price x USDINR, faded 09:05-09:35 | gold gross +5.5 | gold gross +5.0 (beats 95-97% of random) | near miss: below the 6.3 bp GOLDTEN cost (-0.8 / -1.3 net); silver, crude, natgas worse |

D is the only consistent effect: MCX gold opens a few bp away from the world price and corrects in the first half hour, but the
round trip costs more than the correction. At zero brokerage (~4 bp) it would be ~+1 bp a trade, about one trade a day.
