# Every static number, next to a dynamic version (run 2026-09-26, `static_vs_dynamic_study.py`)

Request: "check all static things and check results with dynamic". All options are opt-in parameters of the three backtests (off by default = live behaviour); same judging as the other studies (real halves + 2.7-year proxy years for commodity,
4 calendar years of real data for equity, real halves for currency). "Improves" = beats the static rule in BOTH real halves and >= 2 of 3 proxy years (commodity), >= 4 of 5 years and both halves (equity), both halves (currency).

Static -> dynamic tested: entry levels (ADX, volume surge, |EMA slope|) -> rolling quantile of the same feature over 3 / 10 days; exit shape (TP 1.8R, break-even 0.6R, trail 0.3R) -> the equity scalper's ADX-scaled exit (activation 0.6R/scale, trail 0.3R*scale of the
CURRENT ATR, no fixed TP); TP distance -> x ADX scale; 16-bar time limit -> x ADX scale; 0.35% stop floor -> 1.0 x median ATR of the previous 5 days; all of them together.

## Commodity (net Rs; real first / second half | proxy 2024 / 2025 / 2026)
| Symbol | Static (live) | Best dynamic variant | Verdict |
|---|---|---|---|
| SILVERMIC | +21,964 / +23,958 \| -90,077 / -83,605 / +74,214 | dynamic exit: +26,328 / +32,825 \| -70,687 / -30,078 / -17,265 | improves by the rule (both real halves, 2 of 3 years); but 2026 proxy falls from +74k to -17k and the proxy total is worse (-118k vs -100k) |
| CRUDEOILM | -16,539 / +16,346 \| -38,764 / -58,468 / +71,341 | entry levels 3d: -8,946 / +8,573 \| -35,096 / -67,232 / +83,317 | none improves |
| GOLDTEN | -959 / +18,505 \| -64,733 / -16,463 / +39,440 | entry levels 3d: +8,033 / +15,966 \| -49,045 / -10,512 / +112,043 | none passes (1/2 real halves) |
| NATGASMINI | -17,278 / +3,597 \| -28,682 / -15,160 / +403 | all dynamic: +1,453 / -1,770 \| -22,393 / +8,442 / +5,115 | none passes |
"Everything dynamic" is worse than static on silver, crude and gold in the real halves.

## Equity (49 names, fixed Rs100k, net Rs 2022 / 23 / 24 / 25 / 26; PF)
| Variant | Net by year | Total | PF | Verdict |
|---|---|---|---|---|
| static (live) | +54,480 / +299,201 / +40,325 / +48,353 / +92,468 | +534,828 | 1.21 | - |
| entry levels 3d | +41,528 / +167,601 / -20,702 / -26,122 / +36,209 | +198,514 | 1.07 | much worse |
| entry levels 10d | +38,492 / +102,670 / +62,522 / +61,598 / +86,115 | +351,397 | 1.16 | worse |
| time limit x ADX | +50,265 / +304,701 / +47,602 / +46,444 / +95,963 | +544,975 | 1.21 | 3/5 years, both halves: neutral |
| stop floor dynamic | +54,794 / +282,342 / +49,183 / +57,410 / +108,037 | +551,767 | 1.22 | improves by the rule, but only +3% |
| everything dynamic | +60,913 / +112,598 / +70,966 / +77,243 / +62,651 | +384,371 | 1.17 | worse |
The equity exit is already dynamic (ADX-scaled trail); making the ENTRY levels relative to the recent market removes the absolute-conviction filter and PF falls to 1.07-1.16.

## Currency (USDINR at 10x, real data 06-02..09-24; first half / second half)
static +10,558 / -3,271; dynamic exit +17,573 / -2,064 (both halves better); entry levels 3d +11,754 / -2,840; TP x ADX scale +11,938 / -3,271; time limit x ADX +15,598 / -3,549; stop floor dynamic -1,910 / -4,846; everything dynamic +14,105 / -4,555.
Only 4 months of data (81 trades): a lead, not a verdict.

## Conclusion
Making everything adaptive is worse than the tuned static rule (equity PF 1.21 -> 1.17, entry levels alone 1.07-1.16). The dynamic versions that hold up are narrow: a dynamic EXIT for USDINR (both halves) and for silver (both real halves, but it loses the 2026 proxy
trend), and a dynamic stop floor for equity (+3%). Everything else stays static.
