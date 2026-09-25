# Equity and currency: what drives profit (run 2026-09-25; commodity trading stopped the same day)

## Equity trend rule (49 NIFTY names, 2022-08..2026-09, compounding, real 5x MIS) -- `python3 -m markets.equity.experiments.scaling_study`
- Signal frequency: mean 3.4 / median 2 candidate entries per trading day; 18% of days have none, 34% have <= 1. A near-empty day is normal.
- The edge exists only at high utilisation because Upstox's brokerage is flat-capped (min(0.06%, Rs30) per leg): total exposure (concurrent positions x max margin per position, as a share of the pool)
  100% PF 1.20, 75% PF 1.21, 50% PF 1.20, 34% PF 1.16, 25% PF 1.05, 15% PF 0.77. Below ~25% the rule loses after costs.
- Drawdown falls with exposure: max DD 45% (100%), 36% (75%), 27% (50%), 25% (34%). Total return falls faster (100% +41,426% vs 50% +1,663% compounded -- the absolute figures are NOT credible
  (no market-impact or capacity limit), only the ordering is).
- Risk % (2/4/6/8) changes the result little because sizing is margin-bound; the cap on concurrent positions (2-5) changes almost nothing because the first trade takes the pool.
- Drawdown throttles (halve/quarter size after a 10-20% drawdown) barely reduce max DD (45% -> 38-44%) and cut return heavily.
- Caveat: thresholds were tuned on this same history (3 folds); forward paper results are the real test.

## Currency (USDINR live rule, real Upstox 5-min data 2026-06-02..09-24, Rs100k, risk 10%)
| Leverage | Full period | First half | Second half | Max DD |
|---|---|---|---|---|
| 5x (live) | +Rs507, PF 1.05, ~5 lots | +3.4k, PF 1.64 | -2.9k, PF 0.47 | 4.1% |
| 10x | +Rs7,208, PF 1.36, ~11 lots | +10.6k, PF 2.11 | -3.3k, PF 0.66 | 4.6% |
| 20x | +Rs22,185, PF 1.52, ~24 lots | +26.9k, PF 2.37 | -4.2k, PF 0.77 | 6.6% |
| 42x (Upstox's real cap) | +Rs58,899, PF 1.53, ~65 lots | +68.8k, PF 2.40 | -6.8k, PF 0.82 | 12.4% |
Leverage is the lever for the same reason as equity (flat brokerage cap: 5 lots pay ~Rs120 round trip against a ~0.03% bar). But every leverage loses in the second half (Aug-Sep), so the gain is one regime;
4 months of data only. Configured cap is 5x by the user's earlier instruction; Upstox allows 42x.
