# Multi-Asset Quantitative Trading Framework (Indian Markets)

An institutional-grade, 100% configurable **quantitative trading framework for Indian markets**, focused on **MCX Commodity Futures** (crude oil, gold, natural gas), **NSE Currency Derivatives** (USDINR/EURINR/GBPINR/JPYINR), and **NSE Equities** (Cash/MIS), trading through Upstox.

> **Scope note (2026-09-17):** this project previously also included a Binance USDT-M crypto perpetuals pipeline and an options analytics/trading module (`engine/`, `framework/`, `strategies/options/`, options Greeks/chain tooling). Both were removed to focus entirely on the Indian market path — see git history if you need to recover either. The crypto research findings (why 650+ tested strategy/indicator combinations found no scalping edge, and where the one real edge — funding-rate arbitrage — lived) are preserved in the git history's `docs/CRYPTO_RESEARCH_FINDINGS.md` if useful context for future work.

---

## Table of Contents

1. [Framework Overview](#framework-overview)
2. [System Architecture Flow](#system-architecture-flow)
3. [Directory & Codebase Structure](#directory--codebase-structure)
4. [Supported Instruments & Trading Profiles](#supported-instruments--trading-profiles)
5. [Dynamic Position Sizing & Margin Budgeting](#dynamic-position-sizing--margin-budgeting)
6. [Dynamic Instrument Master Resolution](#dynamic-instrument-master-resolution)
7. [Environment Configuration (`.env`)](#environment-configuration-env)
8. [Unified CLI Cheat Sheet](#unified-cli-cheat-sheet)
9. [Running 24/7 as a systemd Service](#running-247-as-a-systemd-service)
10. [Safety Features (Dry Run)](#safety-features-dry-run)
11. [Telegram Alerts & Equity Curve Chart](#telegram-alerts--equity-curve-chart)
12. [Machine Learning & Microstructure Feature Pipeline](#machine-learning--microstructure-feature-pipeline)
13. [Real MCX Data via Upstox](#real-mcx-data-via-upstox)
14. [Statutory Taxation & Friction Schedule](#statutory-taxation--friction-schedule)
15. [Walk-Forward Backtest Performance](#walk-forward-backtest-performance)
16. [Automated Testing Suite](#automated-testing-suite)

---

## Framework Overview

The framework provides an end-to-end quantitative trading infrastructure:
- **Zero Static Logic**: instruments, lot sizes, contract multipliers, statutory taxes/costs, market hours, and strategy thresholds are all configurable via `.env` or module-level constants, not hardcoded.
- **Dynamic Exchange Master Resolution**: downloads and parses the official Upstox `MCX.csv.gz` daily master, automatically resolving the current front-month contract for each commodity as older ones expire.
- **Production-Grade Risk & Execution**: position sizing via risk budgeting and broker margin/leverage constraints, take-profits, breakeven locks, and trailing stops.
- **Realistic Statutory Cost Model**: itemizes CTT, Brokerage caps, Stamp Duty, Exchange Turnover fees, SEBI fees, 18% GST, and slippage matching the official Upstox brokerage calculator.
- **Layered live-trading kill switch** (`safety_gate.py`): three independent gates (a source-code constant, an `.env` flag, and an explicit armed-state file created via `cli.py arm-live-trading`) must all agree before any code path is allowed to place a real (non-paper) order.

---

## System Architecture Flow

![HFT Trading System Architecture & Execution Flowchart](docs/images/system_architecture.png)

The framework is architected into 5 modular, loosely-coupled layers:
1. **Data Layer**: ingests real-time 5-minute bar feeds via Upstox and parses the official daily MCX master contract.
2. **Feature Engine**: computes microstructure volatility, Parkinson volatility, volume surge ratios, EMA slope, ADX/DMI, VWAP distance.
3. **Strategy & ML Core**: per-symbol LightGBM classifier (currently disabled in favor of rule-based-only entries — see [Machine Learning](#machine-learning--microstructure-feature-pipeline)) combined with ADX/EMA-slope/VWAP/volume-surge trend-expansion rules, calibrated separately per symbol.
4. **Risk Management**: dynamically sizes lots against capital risk % and Upstox MIS margin leverage, managing trailing stops and breakeven locks.
5. **Execution & Audit**: walk-forward backtests, virtual paper execution, SQLite audit logging, and a performance dashboard.

---

## Directory & Codebase Structure

```
backend/
├── broker/                          # Broker Integration & Dynamic Master Feed
│   ├── upstox_broker.py             # Broker client (Quotes, Intraday/Historical Candles & Execution) — always dry_run
│   └── instruments.py               # Dynamic MCX/NSE master downloader & instrument-key resolver
│
├── strategy/                        # Feature engineering & cost models
│   ├── commodity_features.py        # ADX/DMI, VWAP distance, EMA slope, ORB, volume surge, RSI (shared by commodity + currency)
│   ├── commodity_costs.py           # MCX statutory cost engine (CTT, stamp duty, exchange fee, SEBI, GST) + lot sizing
│   └── currency_costs.py            # NSE currency derivatives cost engine (no STT/CTT, different stamp duty/exchange fee) + lot sizing
│
├── ml/                               # Machine Learning
│   ├── train_commodity.py           # LightGBM training/fine-tuning pipeline for MCX Futures
│   ├── experiment_high_winrate.py   # LightGBM/XGBoost/CatBoost/ensemble architecture comparison
│   └── weekly_finetune.py           # Scheduled job: real-data top-up + incremental fine-tune
│
├── systemd/                         # systemd --user unit files (symlinked from ~/.config/systemd/user/)
│   ├── hft-dryrun.service                     # 24/7 paper-trading daemon
│   ├── hft-daily-data-topup.service/.timer    # Nightly real MCX data top-up (00:30 IST)
│   └── hft-weekly-finetune.service/.timer     # Weekly ML fine-tune (Sunday 02:00 IST)
│
├── tests/                           # Unit Testing Suite
│
├── utils/
│   └── market_holidays.py           # Real, auto-updating NSE/CDS/MCX holiday calendar (Upstox's own public API, no hardcoded year/dates)
│
├── safety_gate.py                   # Layered live-trading kill switch (source constant + .env flag + armed-state file)
├── regime_shift.py                  # Dissimilarity Index / out-of-distribution ML input gate (generic, reusable)
├── database.py                      # SQLite persistence: accounts, orders, positions, trades, snapshots
├── cli.py                           # Master Unified CLI
├── backtest_commodity.py            # 5-Minute MCX Commodity Futures Backtest CLI (ENTRY_THRESHOLDS per symbol)
├── backtest_currency.py             # 5-Minute NSE Currency Derivatives Backtest CLI (ENTRY_THRESHOLDS per pair)
├── backtest_natgas_donchian.py      # Donchian trend-following research (negative result, kept for reproducibility)
├── backtest_natgas_meanrev.py       # VWAP/RSI mean-reversion research (negative result, kept for reproducibility)
├── backtest_natgas_patterns.py      # TA-Lib candlestick/oscillator sweep research (negative result, kept for reproducibility)
├── backtest_scalper.py              # NSE Equity Momentum Backtest CLI
├── live_dryrun.py                   # Live 24/7 Paper-Trading Daemon (commodity + currency + equity)
├── real_commodity_data.py           # Real MCX historical data downloader/top-up (via Upstox)
└── real_currency_data.py            # Real NSE currency derivatives historical data downloader/top-up (via Upstox, chunked fetch)
```

---

## Supported Instruments & Trading Profiles

Every symbol below is calibrated with its **own** entry thresholds in `backtest_commodity.py`'s `ENTRY_THRESHOLDS` dict (mirrored in `live_dryrun.py`) — nothing is shared across symbols by assumption; each was validated independently on real data.

### Live-traded (`DRYRUN_SYMBOLS` in `.env`)

| Symbol | Status | Real-data win rate | Notes |
|---|---|---|---|
| **`CRUDEOILM`** | Live | **68.2%**, PF 1.87 | Primary scalper. `min_adx=18` found via a 192-combo real-data sweep (2026-09-17) — improved win rate, net PnL, and max drawdown simultaneously. |
| **`GOLDM`** | Live | **69.2%**, PF 2.72 | Added 2026-09-17 after a 192-combo real-data sweep found 141/144 credible configs profitable (vs. natgas's 0/51) — a robust, not lucky, result. |

### Tested and deliberately NOT traded

| Symbol | Status | Finding |
|---|---|---|
| `NATGASMINI` | Removed 2026-09-17 | **0 real edge found across 5 independent strategy families and 658+ tested configurations**: the original ADX/EMA-slope momentum rules (480 combos), Donchian trend-following (20 combos), VWAP/RSI mean-reversion (48 combos), all 61 TA-Lib candlestick patterns (30 credible), and Stochastic/CCI/Williams %R/MFI/Parabolic SAR/Ichimoku (11 strategies) — only noise-level "wins" (right at the false-positive rate expected from testing that many configurations). Root cause: 85% of the momentum strategy's losses were full stop-outs — entries got reversed against almost immediately, a choppy/mean-reverting market character that also defeated the trend-following and mean-reversion attempts. Real edge in natural gas (per both outside research and this project's own findings) comes from weather-forecast (HDD/CDD) and EIA storage-report data, not price-pattern indicators — a genuinely different, larger project, not a threshold retune. Kept in `ENTRY_THRESHOLDS` as a record; the research scripts (`backtest_natgas_*.py`) remain in the repo so this is reproducible rather than just asserted. |
| `SILVERMIC` / `COPPER` | Surveyed, not added | Baseline (untuned) results: silver near-breakeven, copper a net loser with high drawdown. Neither has had the dedicated calibration sweep gold/crude received. |

### Session window

Full session (10:00–22:30 IST) by default (`DRYRUN_FULL_SESSION=true`) — a real-data sweep found this **more than doubles total net PnL** versus the narrower evening-only US-overlap window (18:30–22:00 IST), at the cost of ~3x more trades, a ~5-point lower win rate, and higher fee drag. Toggle via `.env` if you'd rather trade the narrower, cleaner window.

### NSE Currency Derivatives (`backtest_currency.py`, `ENTRY_THRESHOLDS` per pair)

Same feature engine and entry-rule shape as commodities, with pair-specific thresholds calibrated from a 375-combo real-data sweep per pair (2026-09-18) — `strategy.currency_costs` swaps in the genuinely different NCD_FO fee schedule (no STT/CTT at all on currency derivatives; different stamp duty/exchange-fee rates). Session is 09:00–17:00 IST — no MCX-style evening/US-overlap window (a currency pair has no analogous "second session").

| Pair | Status | Real-data result | Sweep robustness |
|---|---|---|---|
| **`USDINR`** | Live | 45 trades, 48.9% win, PF 3.13, +₹13,854 | **258/258 credible (≥15 trade) combos profitable (100%)** — the most robust result in this project |
| **`GBPINR`** | Live | 17 trades, 41.2% win, PF 2.87, +₹4,679 | **192/192 (100%)** |
| **`EURINR`** | Live | 43 trades, 39.5% win, PF 2.23, +₹9,721 | 201/359 (56%) — still a real, majority-robust edge |
| `JPYINR` | Not added | — | 0/375 combos reached the 15-trade credibility bar — youngest contract, not enough real days yet. Not a negative finding, just insufficient data; revisit once its archive grows. |

**Important shape difference from commodities**: all three live pairs win *under 50%* of trades but are solidly profitable (profit factors 2.2–3.8x) — winners run 2-4x bigger than losers, the opposite payoff shape from crude/gold's 65-70%-win-rate/tight-R:R style. Don't judge these by win rate alone.

---

## Dynamic Position Sizing & Margin Budgeting

The position sizing engine dynamically balances account risk with broker margin limits:

$$\text{Loss per Lot} = \text{Stop Distance} \times \text{Contract Multiplier}$$
$$\text{Lots by Risk} = \left\lfloor \frac{\text{Capital} \times \text{Risk \%}}{\text{Loss per Lot}} \right\rfloor$$
$$\text{Margin Required per Lot} = \frac{\text{Entry Price} \times \text{Multiplier}}{\text{Leverage}}$$
$$\text{Lots by Margin} = \left\lfloor \frac{\text{Capital}}{\text{Margin Required per Lot}} \right\rfloor$$
$$\text{Executed Lots} = \max(1, \min(\text{Lots by Risk}, \text{Lots by Margin}))$$

### Sizing Modes Supported:
* **`--size-mode margin` (Default)**: Strict real-world mode that enforces Upstox / SEBI MIS peak margin limits.
* **`--size-mode risk`**: Pure theoretical risk-budgeted mode where positions scale purely by account risk %.

---

## Environment Configuration (`.env`)

Everything the CLI needs to run with **zero flags** lives in `backend/.env` (copy `backend/.env.example` to start). Any CLI flag you *do* pass overrides its `.env` value for that one run only — `.env` is the default, flags are the override.

### Broker Credentials
| Variable | Purpose |
|---|---|
| `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` | OAuth2 app credentials from [developer.upstox.com](https://developer.upstox.com/). |
| `UPSTOX_REDIRECT_URI` | OAuth2 redirect URL registered with your Upstox app. |
| `UPSTOX_USERNAME` / `UPSTOX_PIN` / `UPSTOX_TOTP_SECRET` | Optional — enables `auth/upstox_auto_login.py`'s headless daily token refresh (Selenium). Leave all three blank to do the manual `python3 -m auth.upstox_auth` refresh instead. **`TOTP_SECRET` is your 2FA seed — combined with `PIN` it's permanent full login access to the real account, treat it like a password.** `ACCESS_TOKEN` itself is *not* stored in `.env` — it's cached at `cache/upstox_token.json` and rewritten daily by the auth flow. |

### Shared Trading Parameters
| Variable | Default | Used by |
|---|---|---|
| `TRADING_CAPITAL` | `100000.0` | `--capital` default for `backtest`, `dryrun`, `reset-db`. |
| `INTRADAY_LEVERAGE` | `4.0` | `--leverage` fallback for all three — used whenever the command-specific `BACKTEST_LEVERAGE`/`DRYRUN_LEVERAGE` below is blank. |
| `TRADING_DIRECTION` | `both` | `--direction` default for `dryrun` (`both` / `long` / `short`). |

### Backtest Defaults (`cli.py backtest`)
| Variable | Default | Meaning |
|---|---|---|
| `BACKTEST_ASSET` | `commodity` | `futures`/`commodity`/`commodities` → MCX; `equity`/`equities`/`cash` → NSE. Currency has no `--asset` value yet — run `backtest_currency.py` directly (see [Unified CLI Cheat Sheet](#unified-cli-cheat-sheet)). |
| `BACKTEST_SYMBOLS` | *(blank)* | Space-separated symbol override, e.g. `CRUDEOILM GOLDM`. Blank = asset's own default list. |
| `BACKTEST_RISK_PCT` | `5.0` | Risk % per trade. |
| `BACKTEST_LEVERAGE` | `4.0` | Margin leverage for backtests specifically. Blank = fall back to `INTRADAY_LEVERAGE`. |
| `BACKTEST_FROM` / `BACKTEST_TO` | *(blank)* | `YYYY-MM-DD` date range. Blank = full available history. |
| `BACKTEST_EQUITY_TOP_N` | `15` | Screener size when `--asset equity` and no explicit symbols. |
| `BACKTEST_FULL_SESSION` | `true` | Commodity only: `true` = full 10:00–22:30 IST session (more total profit, more trades, lower win rate); `false` = evening US-overlap window only (18:30–22:00 IST, fewer/cleaner trades). |
| `BACKTEST_USE_ML_FILTER` | `false` | Commodity only: drop the ML `p_up` condition, keep every other rule-based filter. The ML filter underperformed rule-based-only on real data as of 2026-09-10; revisit once the real archive is substantially larger. |

### Dry Run Defaults (`cli.py dryrun`)
| Variable | Default | Meaning |
|---|---|---|
| `DRYRUN_ASSET` | `commodity` | `commodity` and `equity` are fully wired for live paper trading. |
| `DRYRUN_RISK_PCT` | `10.0` | Risk % per trade. |
| `DRYRUN_LEVERAGE` | `7.0` | Margin leverage for live dry run specifically. Blank = fall back to `INTRADAY_LEVERAGE`. |
| `DRYRUN_INTERVAL` | `30` | Scan interval, seconds. |
| `DRYRUN_EQUITY_TOP_N` | `15` | Screener size for `--asset equity`. Ignored for commodity. |
| `DRYRUN_SYMBOLS` | `CRUDEOILM GOLDM USDINR EURINR GBPINR` | Space-separated symbol override. Currency pairs are auto-detected by symbol name (`USDINR`/`EURINR`/`GBPINR`/`JPYINR`) and routed to the currency cost model + 09:00-17:00 session automatically — no separate `--asset currency` flag needed, just list them alongside commodity symbols. |
| `DRYRUN_FULL_SESSION` | `true` | See [Session window](#session-window) above. |
| `DRYRUN_USE_ML_FILTER` | `false` | Mirrors `BACKTEST_USE_ML_FILTER`. |

### Safety Limits (dry run daemon)
| Variable | Default | Meaning |
|---|---|---|
| `MAX_DAILY_LOSS_PCT` | `5.0` | Halts **new entries** for the rest of the day once realized loss hits this % of the capital the process started the day with. Open positions still exit normally — only new entries stop. Resets automatically at the next trading day. |
| `TOKEN_CHECK_INTERVAL_MIN` | `15` | How often (minutes) the running daemon proactively re-validates its Upstox token; also re-checked immediately if the broker's 401 circuit breaker trips. Auto-refreshes via headless login if `UPSTOX_USERNAME`/`PIN`/`TOTP_SECRET` are set, otherwise alerts you via Telegram to refresh manually. |

### Telegram Alerts (optional)
| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID` | Message your bot once, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID. |

Blank = alerts silently disabled, everything else runs fine without them. See [Telegram Alerts & Equity Curve Chart](#telegram-alerts--equity-curve-chart).

---

## Unified CLI Cheat Sheet

All commands below assume you're in `backend/` with the venv active (or just call `.venv/bin/python3`, which `cli.py` also auto-re-execs into if you run it with the system Python). Every flag shown has an `.env` equivalent from the table above — omit the flag to use whatever's in `.env`.

### 1. `cli.py backtest` — Walk-forward backtest on historical data

```bash
python3 cli.py backtest [--asset {futures,commodity,equity}] [--symbols SYM [SYM ...]]
                         [--capital N] [--risk-pct N] [--leverage N]
                         [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                         [--top-n N] [--full-session]
```
| Flag | Meaning |
|---|---|
| `--asset` | `futures`/`commodity` → `backtest_commodity.py`'s MCX scalper. `equity`/`cash` → `backtest_scalper.py`'s NSE scalper. |
| `--symbols` | Override the asset's default symbol list. |
| `--capital` | Starting capital in ₹. |
| `--risk-pct` | Risk % of capital per trade. |
| `--leverage` | MIS margin leverage multiplier. |
| `--from` / `--to` | Date range filter (`--from-date`/`--to-date` also accepted). |
| `--top-n` | Equity screener size (ignored for commodity). |
| `--full-session` | Commodity only — trade the full 10:00–22:30 IST session instead of just the evening US-overlap window. |

```bash
# Examples
python3 cli.py backtest                                                  # everything from .env
python3 cli.py backtest --asset futures --symbols CRUDEOILM GOLDM --from 2026-08-17 --to 2026-09-17
python3 cli.py backtest --asset equity --symbols RELIANCE INFY TCS
```

### 2. `cli.py dryrun` — Live paper-trading (delegates to `live_dryrun.py`)

```bash
python3 cli.py dryrun [--asset {futures,commodity,equity}] [--symbols SYM [SYM ...]]
                       [--capital N] [--risk-pct N] [--leverage N]
                       [--interval N] [--direction {both,long,short}] [--top-n N]
```
| Flag | Meaning |
|---|---|
| `--asset` | `futures`/`commodity` → MCX 5-min scalper (`CRUDEOILM`/`GOLDM` by default). `equity` → NSE 5-min scalper (screener top-N, 09:15–15:30 IST). |
| `--symbols` | Override the default watchlist. |
| `--interval` | Scan interval, seconds. |
| `--direction` | Restrict to `long` or `short` only, or `both`. |
| `--top-n` | Equity screener size, ignored for commodity. |

```bash
# Examples
python3 cli.py dryrun                                    # everything from .env — this is what the systemd service runs
python3 cli.py dryrun --asset commodity --interval 30
python3 cli.py dryrun --asset equity --top-n 10 --direction long
python3 cli.py dryrun --report                            # dashboard + equity curve chart, no trading
```

This process is a **24/7 daemon**, not a one-shot script: it waits out nights/weekends/holidays and rolls into the next trading day on its own rather than exiting, so it should be run under the systemd service (below), not `nohup`.

### 3. `cli.py report` / `cli.py reset-db`

```bash
python3 cli.py report [--db PATH] [--account ID]                          # dashboard: balance, win rate, open positions, last trades/orders
python3 cli.py reset-db [--db PATH] [--account ID] [--capital N] [--risk-pct N] [--leverage N]   # wipes the DB and starts a fresh paper account
```

### 4. `cli.py arm-live-trading` / `disarm-live-trading` — the layered kill switch

```bash
python3 cli.py arm-live-trading --component {upstox,ALL} --confirm "I UNDERSTAND THIS PLACES REAL ORDERS WITH REAL MONEY"
python3 cli.py disarm-live-trading [--component {upstox,ALL}]
```
Arming the state file is only **one** of three independent gates — `safety_gate.KILL_SWITCH_ENGAGED` must also be hand-edited to `False` in source (and redeployed), and `ALLOW_LIVE_TRADING=true` must be set in `.env`. All three must agree before any broker call is allowed to place a real order; a caller requesting `dry_run=True` is always honored regardless of gate state. See `safety_gate.py`.

### 5. `live_dryrun.py` directly (what `cli.py dryrun` calls under the hood)

Useful when you need a flag `cli.py dryrun` doesn't expose yet (`--long-only`, `--db`, `--account`, `--token`):

```bash
python3 live_dryrun.py [--token TOKEN] [--capital N] [--risk-pct N] [--leverage N]
                        [--top-n N] [--symbols SYM [SYM ...]] [--db PATH] [--account ID]
                        [--commodity] [--long-only] [--direction {both,long,short}]
                        [--interval N] [--report] [--reset-db]
```
| Flag | Meaning |
|---|---|
| `--token` | Explicit Upstox access token (otherwise loaded from config/cache/`.env`/auto-login, in that order). |
| `--commodity` | Switch to MCX mode. Omit for the NSE equity scalper. |
| `--long-only` | Skip all short setups regardless of `--direction`. |
| `--db` / `--account` | Point at a specific SQLite file / account ID — useful for running multiple independent paper accounts side by side (each gets its own [process lock](#safety-features-dry-run)). |
| `--report` | Print the dashboard and save the equity curve chart, then exit — no trading. |
| `--reset-db` | Wipe and reinitialize the DB before starting. |

```bash
# Examples
python3 live_dryrun.py --commodity --capital 100000 --risk-pct 10.0 --leverage 7.0 --interval 30
python3 live_dryrun.py --report --commodity --account DRYRUN_ACCOUNT
python3 live_dryrun.py --reset-db --capital 100000
```

### 6. Backtest scripts directly (`backtest_commodity.py`, `backtest_scalper.py`)

Same engines `cli.py backtest` delegates to, callable directly when you want their full native flag set:

```bash
# MCX Commodity Scalper
python3 backtest_commodity.py --symbols CRUDEOILM --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 backtest_commodity.py --symbols CRUDEOILM GOLDM --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 backtest_commodity.py --symbols CRUDEOILM --capital 100000 --risk-pct 10.0 --size-mode risk   # pure risk-budgeted, unconstrained by margin

# NSE Currency Derivatives Scalper
python3 backtest_currency.py --symbols USDINR --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 backtest_currency.py --symbols USDINR EURINR GBPINR --capital 100000 --risk-pct 10.0 --leverage 7.0

# NSE Equity Scalper
python3 backtest_scalper.py --symbols RELIANCE HDFCBANK TCS INFY --capital 100000 --risk-pct 2.5 --leverage 4.0
python3 backtest_scalper.py --screener --top-n 5 --capital 100000 --risk-pct 2.5 --leverage 4.0
python3 backtest_scalper.py --screener --top-n 5 --long-only --capital 100000 --risk-pct 2.5
```

---

## Running 24/7 as a systemd Service

`live_dryrun.py` is a long-running daemon (it loops across trading days on its own, sleeping through nights/weekends), so it's meant to run under a process supervisor — **not** `nohup`. A `systemd --user` service gives you: survives terminal logout, auto-restarts on crash, centralized logs via `journalctl`, and starts on boot.

### Unit files live in the repo, not just in systemd's directory

All unit files are checked into **`backend/systemd/`** — not hidden away in `~/.config/systemd/user/` where they'd be invisible to the repo and easy to lose track of. `~/.config/systemd/user/` holds only **symlinks** pointing back into `backend/systemd/`, so systemd reads the exact file you see and edit in the project — no separate "deploy" step, no copying, no drift between what's committed and what's running.

| Unit | Type | Purpose | Schedule |
|---|---|---|---|
| `hft-dryrun.service` | persistent daemon | Runs `cli.py dryrun` — the 24/7 paper-trading loop | Always on (`Restart=always`) |
| `hft-daily-data-topup.service` + `.timer` | oneshot + timer | Runs `real_commodity_data.py --topup` (MCX) then `real_currency_data.py --topup` (NSE currency) — two `ExecStart=` lines in one job — appending the day's real candles (1min/5min/15min/1day) to `archive_commodities/*.csv` / `archive_currency/*.csv` for all tracked symbols | Daily, 00:30 IST |
| `hft-weekly-finetune.service` + `.timer` | oneshot + timer | Runs `ml/weekly_finetune.py` — tops up archives, fine-tunes the LightGBM models on new data, restarts the dry-run service to load them | Weekly, Sunday 02:00 IST |

### First-time setup on a new machine

```bash
cd /var/www/html/hft/backend

# 1. Symlink every unit file from the repo into systemd's user directory
mkdir -p ~/.config/systemd/user
for f in systemd/*; do
    ln -s "$(pwd)/$f" ~/.config/systemd/user/"$(basename "$f")"
done

# 2. Let user services survive logout/reboot (one-time, machine-wide)
loginctl enable-linger "$USER"

# 3. Load the units and start everything
systemctl --user daemon-reload
systemctl --user enable --now hft-dryrun.service
systemctl --user enable --now hft-daily-data-topup.timer
systemctl --user enable --now hft-weekly-finetune.timer

# 4. Confirm
systemctl --user status hft-dryrun.service --no-pager
systemctl --user list-timers --all --no-pager
```

### Editing a unit

Edit the file directly in `backend/systemd/` (same as any other project file — visible in your IDE, tracked by git, no secrets in it so safe to commit). Then:

```bash
systemctl --user daemon-reload                # always needed after any unit-file edit
systemctl --user restart hft-dryrun.service     # only for a persistent service; timers just need daemon-reload to pick up a new schedule
```

All trading parameters themselves come from `backend/.env`, not the unit files — edit `.env` and restart `hft-dryrun.service`, no unit-file changes needed for a parameter change.

### Commands

```bash
systemctl --user daemon-reload                          # after creating/editing any unit file
systemctl --user enable hft-dryrun.service               # start automatically on boot/login
systemctl --user start hft-dryrun.service                 # start now
systemctl --user stop hft-dryrun.service                  # graceful stop (SIGTERM -> clean shutdown + Telegram alert)
systemctl --user restart hft-dryrun.service                # e.g. after editing live_dryrun.py or .env
systemctl --user status hft-dryrun.service --no-pager      # is it running, PID, recent log lines
journalctl --user -u hft-dryrun.service -f                  # follow logs live
journalctl --user -u hft-dryrun.service --no-pager -n 100    # last 100 lines

systemctl --user list-timers --all --no-pager                          # next scheduled fire for every timer
systemctl --user start hft-daily-data-topup.service                     # trigger a data top-up right now (don't wait for 00:30 IST)
systemctl --user start hft-weekly-finetune.service                      # trigger a fine-tune right now (don't wait for Sunday)
journalctl --user -u hft-daily-data-topup.service --no-pager -n 50       # last data top-up's output
journalctl --user -u hft-weekly-finetune.service --no-pager -n 50        # last fine-tune's output
```

`Restart=always` on `hft-dryrun.service` is safe with `systemctl stop` — systemd doesn't apply the restart policy to an explicit stop request, only to unexpected exits/crashes.

---

## Safety Features (Dry Run)

Built into `live_dryrun.py`'s `DryRunner`/`main()` — all active by default, no flags needed:

- **Process lock** — refuses to start a second dry-run process for the same `--account`, so a forgotten stray process (or a re-run before the old one exited) can't double-trade the same account. Lock file: `data/.<ACCOUNT_ID>.lock`.
- **Daily-loss kill switch** (`MAX_DAILY_LOSS_PCT`) — halts *new* entries for the rest of the day once realized loss hits the configured % of the day's starting capital. Open positions still get managed/exited normally. Sends a Telegram alert once when tripped, resets automatically the next trading day.
- **Token refresh loop** (`TOKEN_CHECK_INTERVAL_MIN`) — proactively re-validates the Upstox token on a timer, or immediately if the broker's 401 circuit breaker trips. Auto-refreshes via headless login if `UPSTOX_USERNAME`/`PIN`/`TOTP_SECRET` are configured; otherwise alerts via Telegram that a manual `python3 -m auth.upstox_auth` is needed.
- **Graceful shutdown** — both Ctrl-C and `systemctl stop` (SIGTERM) trigger a clean exit with a Telegram alert, not an unhandled crash. The SIGTERM handler is one-shot (re-arms to `SIG_IGN` after the first signal) so a second signal arriving mid-shutdown can't inject a second async exception into the cleanup path.
- **Real, auto-updating holiday calendar** (`utils/market_holidays.py`) — the overnight day-rollover loop used to only skip Sunday (an admitted gap in an earlier version of its own comment); it now also skips Saturday and every real NSE/CDS/MCX trading holiday, sourced live from Upstox's own public holiday API (`GET /v2/market/holidays`, no auth needed) rather than a hand-maintained list. Never hardcodes a year — always reflects whatever year it currently is, cached and refetched automatically once a day (and across a year boundary).
- **Layered live-trading kill switch** (`safety_gate.py`) — see [`cli.py arm-live-trading`](#4-cliparm-live-trading--disarm-live-trading--the-layered-kill-switch) above.

> **Note on scope**: this is a paper-trading system end to end — `UpstoxBroker` is always constructed with `dry_run=True` (enforced independently by `safety_gate.py` even if a caller ever requested otherwise), and no code path currently places real orders. These safety features harden the *paper* daemon (crash alerting, daily-loss discipline, token hygiene); they are prerequisites for eventually going live, not a live-trading switch.

---

## Telegram Alerts & Equity Curve Chart

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (see [Environment Configuration](#environment-configuration-env)) to get:

| Event | Alert |
|---|---|
| Daemon starts / stops | 🟢/🔴 with capital, risk, leverage, direction, symbols |
| Position entered | 📥 symbol, direction, price, qty, SL/TP |
| Position exited | ✅/❌ symbol, direction, exit price, reason, net PnL, balance |
| Scan error / exception | 🔴 with the exception type and message |
| Daily-loss kill switch tripped | 🛑 |
| Token refresh failed | 🔴 |
| End of trading day | 🏁 trade count, total PnL, balance (sourced from the DB, not an in-memory counter — survives a mid-session restart) — **plus the equity curve chart as a photo** |

The equity curve (`utils/chart.py`) is a matplotlib line chart built from `database.py`'s `portfolio_snapshots` table (one row recorded per trade close), aggregated to **one point per calendar day** (that day's end-of-day capital) spanning from the first trading day through today — not a noisy per-trade intraday chart. It's regenerated and sent to Telegram at the end of every trading day, and also saved to `logs/equity_<ACCOUNT_ID>.png` on every `--report` call:

```bash
python3 live_dryrun.py --report --commodity --account DRYRUN_ACCOUNT
# -> prints the dashboard AND saves logs/equity_DRYRUN_ACCOUNT.png
```

---

## Machine Learning & Microstructure Feature Pipeline

The commodity scalper's `p_up` signal comes from a per-symbol LightGBM classifier (`ml/train_commodity.py`), trained on triple-barrier-labeled 5-minute bars from `archive_commodities/*.csv` with the microstructure features in `strategy/commodity_features.py` (ADX/DMI, VWAP distance, EMA slope, ORB breakout distance, volume surge ratio, Parkinson volatility, etc). Trained models are cached at `cache/commodity_models/lgb_<symbol>.pkl` and loaded once at `DryRunner` startup — training/fine-tuning doesn't affect an already-running dry-run process until it's restarted.

**The ML filter is currently disabled** (`BACKTEST_USE_ML_FILTER`/`DRYRUN_USE_ML_FILTER=false`) — a 2026-09-10 backtest comparison found dropping the `p_up` condition (keeping every other rule-based filter) outperformed keeping it by 4-5 points of win rate, consistently, on both the real archive and a synthetic dataset. The model doesn't have enough real data yet to be a net-positive filter; revisit once the real archive (which grows daily via the top-up job) is substantially larger. Also note: `GOLDM`/`SILVERMIC`/`COPPER`'s cached model files predate the current 33-feature schema (28 features) and are incompatible — `backtest_commodity.py` catches this and falls back to rule-based-only automatically, same as when no model file exists.

**Data note**: `archive_commodities/*.csv` holds **genuine historical MCX candles fetched from Upstox** (`real_commodity_data.py`), not synthetic data — see [Real MCX Data via Upstox](#real-mcx-data-via-upstox) below. Because MCX commodity futures are monthly-expiry contracts, real history is capped at roughly a month per contract; it grows by one real trading day nightly via the scheduled top-up. `download_commodity_data.py`'s random-walk generator still exists in the codebase but is no longer used for training/backtesting as of 2026-09-10 — don't reach for it.

### Manual full training
```bash
python3 -m ml.train_commodity --symbol CRUDEOILM   # full from-scratch train, one symbol
python3 -m ml.train_commodity                        # full from-scratch train, all tracked symbols
python3 update_commodity.py                           # top up archive_commodities/*.csv with real Upstox candles, THEN full train
python3 update_commodity.py --no-train                 # top up archives only, skip training
```
Every full train writes a `cache/commodity_models/lgb_<symbol>.meta.json` checkpoint recording the timestamp of the last data row used — that's what fine-tuning (below) uses to know which rows are new.

### Fine-tuning (continued training on new data)
A model trained once and left alone drifts as market microstructure shifts, but re-training from scratch on the full multi-year archive every week is wasteful and throws away everything the model already learned. `ml/train_commodity.py`'s `finetune_commodity_model()` instead **continues training the existing saved model** on only the data added since its last checkpoint — via LightGBM's `init_model` (adds more boosting rounds on top of the existing trees rather than fitting fresh ones). If a symbol has no saved model/checkpoint yet, it transparently falls back to a full train first.

```bash
python3 -m ml.train_commodity --symbol CRUDEOILM --finetune   # fine-tune one symbol on new data since its last checkpoint
python3 -m ml.train_commodity --finetune                        # fine-tune all tracked symbols
```
Skips cleanly (model left untouched) if there are fewer than 100 new labeled rows since the last checkpoint, or if the new data is single-class (no win/loss examples to learn from) — both logged and non-fatal.

### Weekly automated fine-tuning
`ml/weekly_finetune.py` wraps a real-data archive top-up with a fine-tune call in one alerted job, scheduled via `hft-weekly-finetune.service`/`.timer` (in `backend/systemd/`, see [Running 24/7 as a systemd Service](#running-247-as-a-systemd-service) for the full setup) to run **Sunday 02:00 IST** (comfortably after Saturday's MCX close, safely before Monday's session — no open positions to worry about):

```bash
python3 -m ml.weekly_finetune                 # top up archives with real data, fine-tune, restart hft-dryrun.service to load updated models
python3 -m ml.weekly_finetune --no-restart      # same, but leave the running dryrun daemon on its current models
```

It sends a Telegram summary (🧠) with duration and which model files actually changed, or a 🔴 error alert if the job fails (in which case the dryrun daemon is left untouched, still running its last-known-good models).

---

## Real MCX Data via Upstox

`archive_commodities/*.csv` is populated from **genuine historical MCX candles**, fetched via `real_commodity_data.py` using the same Upstox broker/account this bot already live-trades through — no separate data vendor or credentials needed. `download_commodity_data.py`'s earlier random-walk generator is no longer used for training or backtesting (see git history 2026-09-10 for why: everything validated against it — win rates, parameter tuning — was fit to synthetic patterns, not real market behavior).

**Hard constraint**: MCX commodity futures are monthly-expiry contracts, not continuously-listed instruments. Upstox's real history for the *current* active contract only reaches back to that contract's own listing date — typically ~1 month, not years. Requesting further back returns zero candles, not a clipped result. Real history accumulates one genuine trading day at a time via the daily top-up job; there's no way to get more than ~1 month at once without a paid data vendor (TrueData, Global Data Feeds, PortaraCQG all carry real MCX intraday history, but pricing is quote-based, not self-serve). **Every backtest result quoted in this README reflects this real, currently ~32-day, window** — `backtest_commodity.py` prints the actual archive date range it used on every run (never a hardcoded/stale label) specifically so this can't be silently misrepresented.

**Four intervals maintained per symbol**: 1-minute, 5-minute (the one the strategy/ML model actually consumes), 15-minute, and 1-day (which Upstox retains for noticeably longer than intraday — often several months back even when intraday is capped at ~1 month).

```bash
python3 -m real_commodity_data           # full initial backfill, all tracked symbols, all 4 intervals
python3 -m real_commodity_data --topup     # incremental: fetch only candles newer than what's archived (what the daily timer runs)
```

Symbols covered: `CRUDEOILM`/`CRUDEOIL`, `GOLDM`/`GOLD`, `SILVERMIC`/`SILVER`, `COPPER` (base-symbol and mini-contract archive files are kept aligned — `train_commodity.py`/`backtest_commodity.py` look up whichever name they're given via an alias map).

**NSE currency derivatives** (`archive_currency/*.csv`, via `real_currency_data.py`) hit the same real-data wall — same ~1-month-per-contract cap, verified the same way (a wide single-call request to Upstox's history API was found to silently truncate instead of erroring; `real_currency_data.py` fetches in small chunks and unions the results rather than trusting one wide call). No free third-party dataset fills this gap either — checked GitHub and Kaggle directly (2026-09-18): the one GitHub source this project already uses for equity's pre-2022 daily warm-up (`ShabbirHasan1/NSE-Data`) has no currency segment at all, and `jugaad-data`'s official-NSE-bhavcopy library doesn't cover currency derivatives in its roadmap either. Real data here, same as MCX, only grows one real day at a time via the daily top-up job.

```bash
python3 -m real_currency_data           # full initial backfill, all 4 pairs, all 4 intervals
python3 -m real_currency_data --topup     # incremental (what the daily timer runs)
```

---

## Statutory Taxation & Friction Schedule

| Cost Head | MCX Futures (`CRUDEOILM`) | NSE Currency Derivatives | NSE Equity (Intraday) |
|---|---|---|---|
| **CTT / STT** | **0.010%** on Sell turnover | **None — exempt** | **0.025%** on Sell turnover |
| **Brokerage** | **₹20 flat cap** per order leg | **₹20 flat cap** per order leg | **₹20 flat cap** per order leg |
| **Stamp Duty** | **0.002%** on Buy turnover | **0.0001%** (₹10/crore) on Buy turnover | **0.003%** on Buy turnover |
| **Exchange Turnover** | **0.0021%** on total turnover | **0.0009%** on total turnover | **0.00325%** on total turnover |
| **SEBI Regulatory Fee** | **₹10 per Crore** (0.0001%) | **₹10 per Crore** (0.0001%) | **₹10 per Crore** (0.0001%) |
| **GST** | **18%** on (Brokerage + Exch + SEBI) | **18%** on (Brokerage + Exch + SEBI) | **18%** on (Brokerage + Exch + SEBI) |
| **Slippage Buffer** | **½-tick per leg** | **½-tick per leg** | **½-tick per leg** |

Currency derivatives carry the lightest friction of the three — no STT/CTT at all, and a much lower stamp duty (reduced from ₹200/crore to ₹10/crore specifically for currency & interest-rate derivatives).

---

## Walk-Forward Backtest Performance

**Real MCX data, `python3 cli.py report`/`backtest` reproducible on demand.** Every number below is from the actual real archive (currently ~32 calendar days — see [Real MCX Data via Upstox](#real-mcx-data-via-upstox) for why that's the honest ceiling right now, not a limitation of the testing itself). Capital ₹100,000, risk 10%, leverage 7x, full session, ML filter disabled — the exact configuration currently running live.

### `CRUDEOILM` (2026-08-17 to 2026-09-17, 32 days)
- **Trades**: 132 (90 wins / 42 losses)
- **Win Rate**: **68.2%**
- **Profit Factor**: **1.87**
- **Net Realized Profit**: **+₹79,939.63 (+79.94%)**
- **Max Drawdown**: **-13.44%**

### `GOLDM` (2026-08-17 to 2026-09-17, 32 days)
- **Trades**: 39 (27 wins / 12 losses)
- **Win Rate**: **69.2%**
- **Profit Factor**: **2.72**
- **Net Realized Profit**: **+₹58,795.02 (+58.80%)**
- **Max Drawdown**: **-7.89%**

**Read this honestly**: 32 days and ~40-130 trades per symbol is a real, disciplined result (both were found via credible-sample-size sweeps requiring ≥15 trades, not cherry-picked), but it is genuinely a smaller evidence base than the "multi-year" framing that used to be quoted here — that framing was wrong; MCX's monthly-expiry contracts mean there is no multi-year real intraday archive to test against. These numbers will be re-verified and updated as the archive grows via the nightly top-up.

### NSE Currency Derivatives (24-60 days depending on pair, per-pair calibrated thresholds)

Same capital/risk/leverage as above; `backtest_currency.py`, no ML filter (no trained model exists yet for currency).

| Pair | Trades | Win Rate | Profit Factor | Net Realized | Max Drawdown |
|---|---|---|---|---|---|
| `USDINR` | 45 | 48.9% | 3.13 | **+₹13,853.68 (+13.85%)** | -1.46% |
| `EURINR` | 43 | 39.5% | 2.23 | **+₹9,721.30 (+9.72%)** | -3.85% |
| `GBPINR` | 17 | 41.2% | 2.87 | **+₹4,678.85 (+4.68%)** | -1.80% |

Note the win rates: all under 50%, yet all profitable with strong profit factors — see [NSE Currency Derivatives](#nse-currency-derivatives-backtest_currencypy-entry_thresholds-per-pair) above for why. `USDINR`/`GBPINR` had every one of their credible sweep combinations profitable (100%); `EURINR` had 56%. Same small-sample caveat as commodities applies.

---

## Automated Testing Suite

To run all unit tests (commodity statutory cost calculators, NSE scalper pipeline, and the generic dissimilarity-gate ML utility):

```bash
cd backend
.venv/bin/python3 -m unittest discover -s tests
```
