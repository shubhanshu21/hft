# HFT -- paper trading for Indian markets

A small system that trades **paper money** (no real orders) on three markets, records every trade in one SQLite database, and shows
the accounts and backtests on a private website. Strategies are only added after a fair test: rules picked on older data, judged on
newer data the rules never saw, with real costs.

Rewritten 2026-10-30. The old README described the removed dry-run daemon, the 5-minute scalpers, currency and the earlier crypto
strategies; git history has it. `markets/README.md` lists every strategy with its numbers and status.

## What trades now

| Account | Money | Strategy (folder under `backend/markets/`) | When it runs |
|---|---|---|---|
| `MCX_TREND_CARRY` | Rs2.5L, 1.5x | MCX trend + carry: holds the best 3 of 8 commodities' mini futures, re-checked every 10 trading days (`commodity/strategies/tf_daily/trend_carry/`) | Mon-Fri 09:10 and 23:40 IST |
| `CRYPTO_DIP_REVERSAL` | Rs1L | Buys sharp 5-minute dips on 11 coins' perpetual futures; 15% target, 7% stop, middle-band exit, 24 h time stop (`crypto/strategies/tf_5min/dip_reversal/`) | 5 s after every 5-minute close |
| `RESULTS_GAP` | Rs1L | **Results gap** (intraday, 3x): buys results-day gap-ups of 7%+ vs the market that keep rising, 09:30 -> 15:20 (`equity/strategies/tf_intraday/results_gap/`)<br>**Earnings drift** (swing, same account): buys at the open two days after results that beat the market by 4%+ on 2x volume, up to 10 stocks, sells after 40 trading days (`equity/strategies/tf_daily/earnings_drift/`) | Mon-Fri 08:50 (plan), 09:30 / 09:40 (buys), 15:20 / 15:26 (sells) IST |

Prices: Upstox for NSE and MCX, Delta Exchange India for crypto (candles and funding from Binance). Nothing places real orders:
`UpstoxBroker` always runs with `dry_run=True`, and `engine/safety_gate.py` blocks real orders on its own as well.

## Folder layout

```
backend/
  cli.py  .env  requirements.txt  pyproject.toml
  engine/     one runner per account (mcx_trend_carry, crypto_dip_reversal, results_gap + earnings_drift), database, backup_db,
              nightly, research_data (downloads), backtest_api (website API), engine_monitor (Telegram alerts), status, safety_gate
  markets/    <market>/strategies/tf_<timeframe>/<name>/strategy.py, plus each market's costs and data download
  core/       shared logic: fill model (exits.py), backtester, risk, sessions; paths.py holds every filesystem path
  services/   broker/ (Upstox, Delta), auth/ (Upstox auto-login), utils/ (Telegram, holidays, logging)
  deploy/     systemd units (symlinked into ~/.config/systemd/user)
  tests/      unittest suite
  var/        all runtime state, not in git: archive/ (price data), cache/, logs/, db/ (SQLite + backups)
frontend/     React + Vite website (Desk, Accounts, Research, Backtest pages)
```

Code writes only under `backend/var/`. Run modules from `backend/` as `python3 -m <module>`.

## Commands (from `backend/`)

```bash
.venv/bin/python3 -m unittest discover -s tests        # full test suite -- run after every change

python3 cli.py status --account RESULTS_GAP           # one screen: engine state, money, today's trades, open positions, token, last backup
python3 cli.py report --account CRYPTO_DIP_REVERSAL   # trade report
python3 -m engine.results_gap --status                # what an engine would do / holds, without trading
python3 -m engine.backup_db --list | --verify          # backups in var/db/backups/
python3 -m engine.backup_db --restore latest           # waits for running engines; refused while one stays busy
python3 -m engine.research_data <job>                 # downloads: index, nifty-options-oi, banknifty-options-oi, ...
```

`python3 cli.py reset-db` wipes every account (a backup is taken first) -- never run it without deciding to.

## Website

- `https://161.118.190.136/` -- asks for a login (nginx). The username and password are in `~/hft-website-login.txt` on the server.
  The server itself (127.0.0.1) skips the login: the Upstox auto-login's redirect and the smoke tests need that.
- The API (`engine/backtest_api.py`, 127.0.0.1:8000, published by nginx at `/api/`) also needs `X-API-Token` =
  `BACKTEST_ADMIN_TOKEN` for anything that changes saved data: deleting or renaming a backtest, resetting a paper account.
  Saved backtests are never deleted automatically.
- From `frontend/`: `npm test`, `npm run test:smoke [-- <base-url>]` (real Chromium, every page at desktop and phone size),
  deploy = `npm run build` then copy `dist/*` to `/var/www/hft-web/`.

## Services (systemd, user units)

Unit files live in `backend/deploy/systemd/`; `~/.config/systemd/user/` holds symlinks to them. After editing one:
`systemctl --user daemon-reload`.

| Unit | What it does | Schedule |
|---|---|---|
| `hft-mcx-trend-carry.timer` | MCX paper run | Mon-Fri 09:10, 23:40 IST |
| `hft-crypto-dip-reversal.timer` | crypto paper run | every 5 minutes (+5 s) |
| `hft-results-gap.timer` | results gap + earnings drift | Mon-Fri 08:50, 09:30, 09:40, 15:20, 15:26 IST |
| `hft-daily-data-topup.timer` | `engine/nightly.py`: MCX data, MCX futures EOD, all-NSE daily candles, results dates, research data (incl. Nifty / Bank Nifty options), crypto data, DB backup + verify, trade audit. Every step runs even if one fails; failures go to Telegram | 00:30 and 06:30 IST |
| `hft-db-backup.timer` | hourly database snapshot (keeps 48; daily ones keep 14) | hourly |
| `hft-engine-monitor.timer` | Telegram alert when an engine misses a run, fails, or the Upstox login is invalid before an MCX run | every 15 min |
| `hft-backtest-api.service` | the website's API | always on |
| `hft-depth-recorder.service`, `hft-preopen-recorder.timer` | record order-book depth and NSE pre-open quotes (research only) | market hours |

New machine: symlink every file in `deploy/systemd/` into `~/.config/systemd/user/`, run `loginctl enable-linger "$USER"`,
`systemctl --user daemon-reload`, then `systemctl --user enable --now <each timer above> hft-backtest-api.service hft-depth-recorder.service`.

## Settings (`backend/.env`)

The next timer run picks up a change; no restart needed (restart `hft-backtest-api.service` for the website).

| Setting | Used for |
|---|---|
| `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI`, `UPSTOX_USERNAME`, `UPSTOX_PIN`, `UPSTOX_TOTP_SECRET` | Upstox login; the token is refreshed headlessly and cached in `var/cache/upstox_token.json`, never in `.env` |
| `MCX_TREND_CARRY_CAPITAL`, `MCX_TREND_CARRY_EXPOSURE` | MCX account size and exposure |
| `RESULTS_GAP_LEVERAGE` | results gap size (3x), never above Upstox's own limit for the stock |
| `CRYPTO_MAX_TOTAL_NOTIONAL_X`, `CRYPTO_MAX_TOTAL_RISK_PCT` | optional whole-book crypto caps (0 = off) |
| `DELTA_DEMO_API_KEY`, `DELTA_DEMO_API_SECRET`, `DELTA_PLACE_REAL_ORDERS` | Delta demo-account orders (off by default) |
| `ALLOW_LIVE_TRADING` | one of the three switches real orders would need (off) |
| `MARGIN_REFRESH_MIN`, `MARGIN_MAX_AGE_HOURS`, `MARGIN_VERIFY` | how often Upstox margin rates are refreshed |
| `BACKTEST_ADMIN_TOKEN` | token for the website's data-changing calls (unset = those calls are refused) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts (optional) |

`.env` still holds settings of removed parts (`DRYRUN_*`, `CURRENCY_*`, `EQUITY_*`, sandbox keys, ...); nothing reads them.

## Costs used in every backtest and paper trade

| | MCX futures | NSE equity intraday | NSE equity delivery | NSE index options | Crypto perps (Delta) |
|---|---|---|---|---|---|
| Brokerage | Rs30 per order (Upstox Plus) | Rs30 or 0.05% | Rs30 or 0.1% + Rs20 DP per sale | Rs30 per order | 0.05% taker / 0.02% maker |
| Tax on the sale | CTT 0.01% | STT 0.025% | STT 0.1% both sides | STT 0.15% of premium (0.10% before Apr 2026) | -- |
| Other | stamp, exchange, SEBI, GST 18% | stamp 0.003%, exchange, SEBI, GST | stamp 0.015%, exchange, SEBI, GST | exchange 0.0353%, stamp 0.003%, GST | GST 18% on fees, real funding |
| Slippage | half the bid-ask spread | 0.1% a side | 5 bp a side | max(Rs0.10, 0.5% of premium) | per coin |

Code: `markets/commodity/costs.py`, the `costs()` / `delivery_costs()` of the equity strategies, and constants in the crypto strategy.

## Fill model (read before trusting any backtest)

A stop fills at the stop price only if the bar opened on the right side of it; otherwise it fills at that open. When one bar reaches
both stop and target, the stop comes first unless the 1-minute path says otherwise. Code: `core/exits.py`, `core/minute_bars.py`.
Results from before 2026-10-07 filled every stop at the stop price and are not valid.
