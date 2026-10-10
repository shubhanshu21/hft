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
