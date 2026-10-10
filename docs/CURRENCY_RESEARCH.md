# Rupee currency futures: can they be traded at a profit? (2026-10-10)

Scope: the NSE rupee futures Upstox offers -- USDINR, EURINR, GBPINR, JPYINR. Data: every NSE currency future, every trading day,
2012-01-02 to 2026-10-09 (NSE bhavcopy, `var/archive/currency/futures_daily.csv.gz`, `python3 -m engine.research_data currency-daily`).
Upstox's own history is ~4 months, so this is the first test of currency rules across many years and regimes (2013 rupee crisis,
2016 Brexit, 2020, 2022, the managed 2023 market, the 2024 RBI rule, the 2026 rupee slide).

**Answer: no.** None of the eight rules below makes money after costs in both halves of the 14 years. The 5-minute scalping searches
(earlier, ~80k variants incl. USDINR) found no edge either. The one recurring effect is in the opening trade, which cannot be traded.

## Method

Rules were written down before the results were seen (T1-T5); T4b and T6 were added after, and are marked so. The contract held
each day is the one with the largest open interest; returns are measured on one contract and a roll is charged as a round trip. A
signal known at a day's close trades at the next OPEN; intraday tests exit at the settlement price (the last half-hour's average).
Halves: discovery 2012-2018, validation 2019-2026. Luck control: the same trades with 1,000 random directions.

Costs (round trip, `markets/currency/costs.py`; Upstox's Rs30 per order cap is a fixed Rs70.8 per round trip, so size matters):

| USDINR 1 lot | 2 lots | 5 lots | 10 lots | 20 lots | EURINR 2 lots | GBPINR 2 lots | JPYINR 3 lots |
|---|---|---|---|---|---|---|---|
| 8.3 bp | 4.7 bp | 2.5 bp | 1.75 bp | 1.4 bp | 10.9 bp | 10.1 bp | 11.4 bp |

## Results (net % of notional per year; 1x notional, costs at ~Rs2 lakh per pair)

| Rule | Pair | 2012-18 | 2019-26 | Verdict |
|---|---|---|---|---|
| T1 carry: always short (earn the forward premium) | USDINR | +1.0 | -1.6 | fails |
| T1 reverse: always long | USDINR | -2.2 | +0.5 | fails |
| T1 carry (robustness) | EURINR / GBPINR | +2.3 / +3.1 | -1.0 / -4.0 | fails |
| T1 carry (robustness) | JPYINR | +5.5 | +4.8 | positive, but a single bet on a falling yen (lost in 2025 and 2026), and JPYINR now trades ~10 contracts a day |
| T2 trend, sign of 60-day return | USDINR / EURINR / GBPINR / JPYINR | +0.8 / -0.8 / -0.7 / +3.5 | -1.4 / -4.4 / -4.7 / -3.5 | fails (20/120/250 days: no better) |
| T3 reverse a big day (> 1 sd) for one day | all four | -1.7 to -10.6 | -3.3 to -7.0 | fails |
| T4 fade the opening gap (> 0.5 sd) to the settle | USDINR | -0.9 (gross +3.5 bp/trade) | -0.5 (gross +3.7 bp/trade) | fails: the gross edge is the opening print, see below |
| T4 (same) | EURINR / GBPINR / JPYINR | -11 to -15 | -9.6 to -11.4 | fails (their gaps continue, and cost 10+ bp) |
| T5 long USDINR over the last 3 days of the month | USDINR | -1.7 | -0.9 | fails |
| T4b (post-hoc) resting limit orders fading 0.5 sd moves | USDINR | -13.5 at 20 lots | -9.0 at 20 lots | fails, in all 15 years |
| T6 (post-hoc) the opposite: stop-order breakout at 0.5 sd, no trade on gap days | USDINR | gross -0.4 bp/trade | gross +0.1 bp/trade | fails at every threshold (0.25-1.0 sd) |
| C1 calendar spread: fade a 1.5 sd stretch in the near/far premium (the trade broker courses teach) | USDINR | gross -0.6 bp/trade | gross -0.3 bp/trade | fails: cost 3.3 bp (20 lots) / 9.9 bp (2 lots) per spread; thresholds 1.0 and 2.0 sd no better |

**The opening print.** USDINR's gap fade earns ~3.5 bp a trade gross, in both halves, beating 96.6% of random directions. Matched
against 1-minute prices (88 days, 2026-06 to 10), the first minute trades a median of 22 contracts, and price moves 1.6 bp back by
09:01 and 2.8 bp by 09:05. Entering at 09:05 instead of the opening trade leaves ~0 bp. The edge is the noise of a thin opening trade,
which an order sent at the open does not get. Whether a resting order at the open could capture it is an order-book question: the depth
recorder has captured USDINR's book every day since 2026-10-09 (see `docs/RESEARCH_DATA.md`); study it around 2026-11-06.

**The forward premium (what a short earns) against the rupee's fall (what it loses), % per year** (the two monthly contracts with the most
open interest; the weekly contracts listed since 2023 barely trade and are left out):

| | 2012-16 | 2017-22 | 2023-25 | 2026 |
|---|---|---|---|---|
| premium | 6-7 | 3.6-4.5 | 1.6-2.5 | ~3.6 (about 5 in October) |
| rupee fall | 3-13 (avg ~4.9) | -6 to +11 (avg ~3.5) | 0.5-5 | 7.4 so far |

Over the 14 years the two roughly cancel: carry is a bet on the rupee, not an edge.

## Who makes money in this market (internet search, 2026-10-10)

- Proprietary traders were 67.7% of currency-derivatives activity in 2023-24 (SEBI, via Business Standard): arbitrage between exchanges
  and against bank forwards, and market making, run by algorithms. Banks earn the spread on their clients' flows. Exporters and importers
  hedge, i.e. accept a loss on the future to protect their business.
- Broker courses teach calendar spreads and expiry convergence to the RBI reference rate: C1 above, ~0 gross.
- RBI policy days: an event study found policy surprises move bond yields and stocks but not the rupee, since RBI steadies it.
- The offshore market (NDF, closed to resident retail) leads the onshore open, and RBI often intervenes before 09:00 (Reuters/Bloomberg
  traders' reports). This fits the finding above that the only effect is at the opening print.

The route that matches how professionals earn here is supplying liquidity at the open, which needs the order book (recorded since
2026-10-09). USDINR options (option selling) are the one currency instrument not tested; options were removed from this project once
before, so only with the user's go-ahead.

## Machine learning, 5-minute scalping on USDINR, EURINR, GBPINR (2026-10-10)

Data: Upstox 5-minute bars of the liquid contract only (USDINR 2026-08-25 to 10-09, 31 days; EURINR / GBPINR 2026-08-21 to 10-09,
33 days; JPYINR left out, ~45 contracts a day). Upstox and Zerodha keep no intraday history of expired currency contracts, and no free
source was found, so this is all there is. Contract switches were back-adjusted with NSE settlement prices; 60 stray opening prints in
the thin pairs were replaced. Inputs only, never traded: Yahoo 5-minute EUR/USD, GBP/USD, USD/JPY, dollar index, USD/CNH, offshore USD/INR.

Method, fixed before running: one pooled model, 47 inputs (own returns, VWAP distance, range, volume, time of day, the other pairs'
returns, global returns, and the parity gap = NSE EURINR vs EUR/USD x USDINR, likewise GBPINR, USDINR vs offshore spot). LightGBM and
ridge regression, parameters not tuned. Walk-forward: each test day is predicted by a model trained on earlier days only. Development
2026-08-21 to 09-25 (24 days); the last two weeks (2026-09-28 to 10-09, 9 days) held out and run once at the end. Trade when the
prediction exceeds the round-trip cost.

| | USDINR | EURINR | GBPINR |
|---|---|---|---|
| correlation of prediction with the next 15-30 min move, unseen days (dev / holdout) | -0.08 to -0.01 / -0.08 to 0.05 | 0.42-0.53 / 0.42-0.46 | 0.38-0.46 / 0.34-0.41 |
| best 10% of predictions, average move (15-30 min) | < 1 bp | 5-9 bp | 4-5 bp |
| round-trip cost with market orders | 1.4 bp (20 lots) | 10.9 bp (2 lots, 7.5 bp of it spread) | 10.1 bp |
| result, market orders (the rule fixed in advance) | loses in every variant | 0-3 trades in 9 days: too few | 0-4 trades: too few |
| upper bound, both legs as limit orders that always fill (added after the results) | -- | +1.2 to +3.5 bp a trade, 60-110 trades per period, dev AND holdout | +0.4 to +2.8 bp a trade |

USDINR at 5 minutes is not predictable from any of this (as the earlier scalping searches found). EURINR and GBPINR are: the main
source is the parity gap (single-input correlation -0.43 EURINR, -0.38 GBPINR). The thin rupee crosses lag EUR/USD x USDINR and
catch up within 15-30 minutes. It is not bid-ask bounce: GBPINR's own last move has ~0 correlation. But the predictable move is
smaller than the spread, so crossing the spread loses; it can pay only by resting limit orders on the side the gap says, i.e. making the
market around the fair value, which is how the proprietary firms earn here. Whether such orders fill, and how often they fill just
before the price runs against them, can only be answered from the order book: EURINR and GBPINR were added to the depth recorder from
2026-10-12, and EUR/USD, GBP/USD at 1 minute to the nightly global download. Re-test with ~15 trading days of book data
(~2026-11-02): simulate resting orders with queue position against the recorded trades, then paper-trade if it holds. A live EUR/USD
price is also needed to trade it (Upstox has none).

Luck control (20 runs with each day's labels swapped for another day's, development period): EURINR / GBPINR correlations of
0.38-0.53 against a shuffled maximum of 0.05-0.12, beating all 20 runs for every model and horizon; their best-10% moves of 4-8 bp
against a shuffled maximum of 1.2-2.2 bp. USDINR's 5-minute correlation (0.06-0.07) beats 95-100% of shuffles but is worth 0.2 bp
against a 1.4 bp cost; its 15-30 minute correlations are at chance.

### Round 2: real spread and a ten-model contest (2026-10-10)

The 10.9 / 10.1 bp costs above used a 7.5 bp EURINR spread from 90 quote snapshots on two days. The spread actually paid in trades
(Roll estimate from 30 days of 1-minute prices) is 3.9 bp EURINR, 3.1 bp GBPINR (USDINR 1.0 bp, matching its quotes). Realistic
market-order round trips: EURINR 7.4 bp at 2 lots / 5.4 bp at 5 lots, GBPINR 6.1 / 4.5 bp.

Ten models (ridge, elastic net, LightGBM, XGBoost, extra trees, neural net, k-nearest neighbours, logistic, a rank-average ensemble,
and the no-ML rule "fade the parity gap") x horizons 15 / 30 / 60 minutes, all walk-forward, with each model's trade threshold also
chosen walk-forward from its own earlier out-of-sample days. The late days (09-28..10-09) were seen once in round 1, so they are a
second look, not a clean holdout.

| configurations net-positive (EURINR + GBPINR, of 30) | development | late days | both |
|---|---|---|---|
| cost model (10-11 bp) | 0 | 4 | 0 |
| 2 lots, real spread | 7 | 24 | 6 |
| 5 lots, real spread | 18 | 30 | 18 |

Best (30-minute horizon, both periods together): ensemble EURINR at 5 lots +2.1 bp a trade, 117 trades, t = 3.0, 12 of 16 days
positive; parity rule GBPINR at 5 lots +2.5 bp, 65 trades, t = 2.3; elastic net (pre-registered winner) EURINR at 2 lots +1.9 bp, 41
trades, t = 1.3. The plain parity rule is about as good as the models: the edge is the parity gap, ML adds little. USDINR: no model
trades it profitably.

Open risks, all answerable only live: the spread at the moment of a signal (a gap may open exactly when the book is wide), whether
5 lots fill at the touch in a ~1,800-contract-a-day market, and a live EUR/USD price. Next step: shadow-trade EURINR/GBPINR from
2026-10-12 (signals logged live, filled against the recorded bid/ask), decide on paper trading after ~3 weeks.

## Deployed to paper trading: `markets/currency/strategies/tf_5min/parity` (from 2026-10-12)

Rule (no ML -- the plain gap did as well as the models): on each completed 5-minute bar, gap = log(cross) - log(EUR/USD or GBP/USD,
Yahoo) - log(NSE USDINR), minus its median over the last 60 bars (restarted after an overnight jump > 25 bp, i.e. a contract roll on one
leg). EURINR |gap| > 10 bp, GBPINR > 12 bp: fade it, 5 lots, hold 30 minutes, protective stop 25 bp, entries 09:20-15:50. Thresholds
chosen on 2026-08-21..09-16 only. Paper fills walk the live 5-level book (the crosses often show 1-3 lots at the best price), so the
paper P&L pays the real spread; costs() adds brokerage, exchange, SEBI, stamp duty and GST.

Backtest of that exact code on the raw archives (`python3 -m markets.currency.strategies.tf_5min.parity.backtest`; entry next open + half the Roll spread,
exit 30 min later - half spread):

| 5 lots | trades | win % | net bp a trade | net Rs | t |
|---|---|---|---|---|---|
| threshold-choosing days (to 09-16) | 77 | 58 | +2.2 | +9,562 | 2.3 |
| test days (09-17..10-09) | 57 | 61 | +2.7 | +9,312 | 2.3 |
| all (31 days) | 134 | 60 | +2.4 | +18,873 (fees 11,396) | 3.3 |
| same, 2 lots | 134 | 48 | +0.6 | +1,857 | 0.8 |

7 of 8 weeks positive. At 2 lots the fixed Rs70.8 brokerage takes almost all of it. Judge it on its paper trades from 2026-10-12.

## What changed in 2024: only USDINR is liquid

RBI's 2024 rule (rupee currency derivatives need an underlying exposure) emptied the other pairs. Median daily volume of the contract
held: EURINR 210,780 (2022) -> 1,845 (2026); GBPINR 239,293 -> 2,246; JPYINR 68,190 -> 16. USDINR fell from 2.7 million to ~0.4-0.6
million and remains liquid. On 2026-10-10 RBI cut the limit for positions without proof of exposure from US$100 million to US$5 million
(reported by Kotak Neo); our sizes (US$1-20 thousand) are far below it, but confirm with Upstox what declaration it needs before any
real trade.

## Re-run

    python3 -m engine.research_data currency-daily        # tops up nightly as part of `research_data all`
