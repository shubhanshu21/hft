# Research data: what institutions use that we lacked, and what we now collect (2026-10-09)

Institutions trade on more than price candles: the order book, derivatives positioning (open interest), who holds what (participant
data), market context (index, volatility), and long, fine-grained histories. This lists what we now download or record, and what stays
out of reach. Storage is compressed: the disk has about 3 GB free.

## Recorded from now on (cannot be downloaded later)

| Data | Instruments | Where | Service |
|---|---|---|---|
| Order book: 5 best bid/ask prices and quantities, last trade, volume, total buy/sell quantity, open interest; at most one row per second | USDINR, SILVERMIC, GOLDTEN, CRUDEOILM, NIFTY and BANKNIFTY front futures, RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS | `var/archive/depth/<SYMBOL>/<day>.csv.gz` | `hft-depth-recorder.service` (`engine/depth_recorder.py`), since 2026-10-09 |

Upstox keeps no depth history, so every day not recorded is lost. Change the instrument list with `DEPTH_SYMBOLS` in `.env`. A liquid
contract adds about 1 MB a day compressed; check disk monthly.

## Downloaded (history) and topped up nightly (`python3 -m engine.research_data all`, a step of the nightly job)

| Data | Coverage | Where | Why |
|---|---|---|---|
| 1-minute candles, 49 NIFTY50 stocks | 2022-01 onward (was 2026-05 onward) | `var/archive/equity/<SYM>_1minute.csv.gz` | stop and target fills settled minute by minute in every year of the stock backtests, not only since May 2026 |
| Nifty 50, Nifty Bank, India VIX, 1-minute | 2022-01 onward | `var/archive/index/<NAME>_1minute.csv.gz` | market context (index trend, fear gauge) for any strategy |
| NIFTY and BANKNIFTY front-month futures, 1-minute **with open interest** | 2024-10 onward (Upstox expired-contract data) | `var/archive/index_futures/<NAME>_FUT_1minute_oi.csv.gz` | futures positioning: OI build-up / unwinding with price |
| NSE participant-wise open interest and volume (Client, DII, FII, Pro) in equity derivatives, daily | 2020-01 onward | `var/archive/nse/participant_oi/`, `participant_vol/` | how institutions are positioned in index futures and options |
| Nifty weekly options within +-3% of the index: 5-minute OHLC, volume, open interest, per expiry | 2024-10 onward | `var/archive/options/NIFTY/<expiry>.csv.gz` | options positioning (put/call OI, OI walls, changes) as an input; not for trading options |

Upstox calls are paced at 1.2 s (`UPSTOX_PACE_S`) to stay inside the 2,000-per-30-minutes limit the trading daemon shares; the first full
backfill takes about four hours, the nightly top-up a few minutes. Readers of 1-minute files (`core/minute_bars.py`,
`engine/exit_replay.py`) accept the `.csv.gz` form.

## Still out of reach

| Data | Why not |
|---|---|
| Tick-by-tick order book (every order added, changed, cancelled) | exchange TBT feed for trading members, needs co-location-grade infrastructure |
| 30-level depth | Upstox Plus subscription (paid) |
| MCX and currency intraday history before ~4 months ago | Upstox serves only current contracts and its expired-contract data covers NSE index derivatives only; paid data vendors |
| Client order flow (banks' FX flows, brokers' order books) | private to those firms |
| Global context (dollar index, Brent, US futures) at 1 minute | Dukascopy downloader exists (`services/data/dukascopy.py`) but timed out on 2026-10-09; retry later |
