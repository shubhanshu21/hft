# Leading signals for commodity moves: literature and tests (run 2026-09-25, `leading_signal_study.py`)

Question: the live rule enters after the volume spike (e.g. the 2026-09-25 silver long at RSI 74 that reversed). Is there a signal that leads the move?
Each candidate below comes from published work and is tested on our data (real MCX 1-minute archive 2026-05..09; Dukascopy global 1-minute 2024-01..2026-09 as proxy). Round-trip cost hurdle ~6 bps of notional.

## Literature (searched 2026-09-25)
- Order-flow / order-book imbalance predicts price changes over seconds to tens of seconds, near-linearly (Cont, Kukanov & Stoikov); VPIN predicts short-term toxicity-driven volatility (Easley et al.). Needs level-2 data; horizon is far below our 30-second scan and REST latency.
- Intraday momentum: the first half-hour return predicts the last half-hour (Gao, Han, Li & Zhou 2018, ETFs; crude oil version, Energy Economics 2021: only the first half-hour predicts the last; on EIA inventory days the third half-hour predicts the last).
- Compression -> expansion (Crabel NR7, Bollinger squeeze): expansion follows contraction more often than chance, direction is not called; the modest edge is asserted rather than rigorously replicated.
- Scheduled announcements move prices: EIA crude inventory surprises have an immediate inverse effect on WTI; gold reacts to FOMC surprises for 5+ minutes.
- MCX contracts track NYMEX/COMEX in rupees (~0.95+ correlation); no published intraday lead-lag study of NYMEX -> MCX was found.
- A falsification study of 14 OHLCV intraday momentum signals on Nasdaq micro futures (arXiv 2605.04004) found none survived costs + out-of-sample + stability.

## Results
T1 global -> MCX lead-lag (79-80k aligned 1-minute bars per symbol): MCX follows global almost entirely within the same minute (corr 0.48 gold/natgas, 0.55 crude, 0.70 silver); the next minute adds only 0.08-0.13,
minute 2 0.02-0.05, later ~0. After a >= 20 bps global 1-minute move, MCX's next 5 minutes go the same way by only +3.4 bps crude (hit 41%), +0.8 silver, +4.4 gold (n=185, hit 57%), +3.6 natgas -- all below the 6 bps hurdle,
and the catch-up happens within seconds, which a 30-second REST scan cannot capture.
T2 first half-hour -> last half-hour (714 days): crude corr -0.06 (t=-1.3), silver -0.07 (t=-1.0), gold +0.04, natgas +0.07 (+3.95 bps, t=+1.6): no significant effect; the published crude result does not replicate on 2024-26. The last half-hour (23:00-23:30 IST)
is also outside our MIS square-off (22:45). The 21:30-22:45 IST window shows nothing (|t| <= 0.4).
T3 EIA Wednesdays (143 days): third half-hour -> last half-hour is a REVERSAL, mean -7.5 bps (t=-2.2, hit 43%), the opposite sign to the paper; single test of several, not tradable in our hours.
T4 compression -> expansion (15-minute bars, 20-bar bandwidth percentile): the reverse of the folklore. After a squeeze the next hour's range is 0.54-0.73x the average (quiet stays quiet); after a wide-band period it is 1.36-1.41x
(volatility clusters). Direction after a squeeze is not called (hit 38-43%). Practical reading: filter TO active volatility, not to compression.
Follow-up screen (post-hoc, TREND trades split by bandwidth percentile at entry, >= 30% / >= 50% kept): mixed and mostly not helpful -- the rule already enters in active markets (few trades dropped), e.g. gold real first half drops
+5.3k of winners; crude proxy 2025 drops -6.9k of -58k; silver proxy 2026 drops -44k of losers but real halves gain little. Not a fix; not deployed.

## Conclusion
No published leading signal is both predictive on our data and tradable with our cost and latency. What is robust: volatility clusters; the global price leads MCX by seconds (an execution-speed edge, not a strategy for a 30-second polling daemon);
order-book imbalance is the one academically supported short-horizon predictor but needs depth history we do not have. Next step if wanted: log Upstox market-depth snapshots (bid/ask quantities) alongside the spread sampler for 3-4 weeks, then test order-flow imbalance
against forward 1-5 minute returns with the same cost hurdle.
