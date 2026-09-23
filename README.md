# Multi-Asset Quantitative Trading Framework (Indian Markets)

An institutional-grade, 100% configurable **quantitative trading framework for Indian markets**, focused on **MCX Commodity Futures** (crude oil, gold, natural gas), **NSE Currency Derivatives** (USDINR/EURINR/GBPINR/JPYINR), and **NSE Equities** (Cash/MIS), trading through Upstox.

> **Scope note (2026-09-17):** this project previously also included a Binance USDT-M crypto perpetuals pipeline and an options analytics/trading module (`engine/`, `framework/`, `strategies/options/`, options Greeks/chain tooling). Both were removed to focus entirely on the Indian market path — see git history if you need to recover either. The crypto research findings (why 650+ tested strategy/indicator combinations found no scalping edge, and where the one real edge — funding-rate arbitrage — lived) are preserved in the git history's `docs/CRYPTO_RESEARCH_FINDINGS.md` if useful context for future work.
>
> **Equity note (2026-09-19):** the NSE equity scalper was ALSO removed once (2026-09-18) on the same "no real OOS edge" grounds, then rebuilt from scratch the next day once the root cause was found: the original universe was hand-picked *because* each stock already backtested well (a circular, survivorship-biased selection, not a real edge), and its untuned entry rules generated so many small trades that Upstox's flat brokerage cap exceeded the average trade's gross profit — capital was mathematically guaranteed to decay to zero regardless of risk-sizing. The rebuild fixed both: a fixed, performance-blind NIFTY50 universe (49 names — see [Supported Instruments & Trading Profiles](#supported-instruments--trading-profiles)) and higher-conviction thresholds validated across three independent train/test folds. It's live in paper trading now.

---

## Table of Contents

1. [Framework Overview](#framework-overview)
2. [System Architecture Flow](#system-architecture-flow)
3. [Directory & Codebase Structure](#directory--codebase-structure)
4. [Supported Instruments & Trading Profiles](#supported-instruments--trading-profiles)
5. [Dynamic Position Sizing & Margin Budgeting](#dynamic-position-sizing--margin-budgeting)
6. [Dynamic Instrument Master Resolution](#dynamic-instrument-master-resolution)
7. [Environment Configuration (`.env`)](#environment-configuration-env)
8. [Adding a strategy](#adding-a-strategy)
9. [Unified CLI Cheat Sheet](#unified-cli-cheat-sheet)
10. [Running 24/7 as a systemd Service](#running-247-as-a-systemd-service)
11. [Safety Features (Dry Run)](#safety-features-dry-run)
12. [Telegram Alerts & Equity Curve Chart](#telegram-alerts--equity-curve-chart)
13. [Quantitative Strategy & Autocorrelation Regime Architecture](#quantitative-strategy--autocorrelation-regime-architecture)
14. [Real Market Data Ingestion via Upstox](#real-market-data-ingestion-via-upstox)
15. [Statutory Taxation & Friction Schedule](#statutory-taxation--friction-schedule)
16. [Walk-Forward Backtest Performance](#walk-forward-backtest-performance)
17. [Automated Testing Suite](#automated-testing-suite)

---

## Framework Overview

The framework provides an end-to-end quantitative trading infrastructure:
- **Zero Static Logic**: instruments, lot sizes, contract multipliers, statutory taxes/costs, market hours, and strategy thresholds are all configurable via `.env` or module-level constants, not hardcoded.
- **Dynamic Exchange Master Resolution**: downloads and parses the official Upstox `MCX.csv.gz` daily master, automatically resolving the current front-month contract for each commodity as older ones expire.
- **Production-Grade Risk & Execution**: position sizing via risk budgeting and broker margin/leverage constraints, take-profits, breakeven locks, and trailing stops.
- **Realistic Statutory Cost Model**: itemizes CTT, Brokerage caps, Stamp Duty, Exchange Turnover fees, SEBI fees, 18% GST, and slippage matching the official Upstox brokerage calculator.
- **Layered live-trading kill switch** (`engine/safety_gate.py`): three independent gates (a source-code constant, an `.env` flag, and an explicit armed-state file created via `cli.py arm-live-trading`) must all agree before any code path is allowed to place a real (non-paper) order.

---

## System Architecture Flow

![HFT Trading System Architecture & Execution Flowchart](docs/images/system_architecture.png)

The framework is architected into 5 modular, production-grade layers:
1. **Real-Time Data Ingestion Layer**: ingests real-time 5-minute bar feeds via Upstox and parses the official daily MCX/NSE master contracts.
2. **Microstructure Feature Engine**: computes Parkinson volatility, volume surge ratios, EMA slope, ADX/DMI trend expansion, and VWAP distance.
3. **Quantitative Signal Engine & Regime Gate**: pure rule-based momentum breakout core with multi-symbol daily-return autocorrelation regime gates (blocks trading on choppy/mean-reverting days).
4. **Risk Management & Position Sizing**: dynamically sizes lots/shares against segment capital risk % and Upstox MIS margin leverage, managing trailing stops, anti-martingale drawdown scaling, and portfolio heat caps.
5. **Execution & Audit Core**: walk-forward backtests, virtual paper execution, SQLite audit logging, performance dashboards, and automated Telegram notifications.

---

## Directory & Codebase Structure

```
backend/
├── cli.py                            # Unified CLI (backtest, dryrun, report, reset-db, arm/disarm live trading)
├── .env / .env.example / requirements.txt
│
├── engine/                           # What runs: runners + plumbing
│   ├── live_dryrun.py                # 24/7 paper-trading daemon (commodity + currency + equity)
│   ├── live_trading.py               # Real-order engine -- gated by the multi-layer safety switch
│   ├── safety_gate.py                # Layered live-trading kill switch (source constant + .env flag + armed-state file)
│   ├── database.py                   # SQLite persistence: accounts, orders, positions, trades, snapshots
│   ├── config.py                     # Upstox credentials/token handling
│   └── backup_db.py                  # Daily SQLite backup (14-day retention)
│
├── markets/                          # What trades: one folder per market
│   ├── commodity/                    # MCX
│   │   ├── costs.py  features.py  data.py        # shared by every commodity strategy (statutory costs, indicators, Upstox top-up)
│   │   ├── scalping/                 # entry_signal.py (also serves currency), backtest.py -- the live strategy
│   │   └── experiments/              # throwaway research (EMA crossover)
│   ├── currency/                     # NSE currency derivatives -- same shape (costs, data, scalping/, experiments/)
│   ├── equity/                       # NSE NIFTY50 -- costs, features, universe, data, scalping/ (own entry_signal + backtest)
│   └── index_futures/                # costs, data, backtest
│                                     # a new strategy (e.g. swing) = a new sibling folder of scalping/
│
├── core/                             # Market-agnostic logic
│   ├── regime.py  regime_shift.py    # daily-return autocorrelation gate; out-of-distribution gate
│   ├── slippage.py                   # real bid/ask spread -> adaptive slippage
│   ├── sector_correlation.py  base_engine.py
│   ├── strategy.py                   # THE strategy contract (Strategy, Signal, contexts) -- see docs/ADDING_A_STRATEGY.md
│   ├── registry.py                   # auto-discovers markets/*/*/strategy.py; picks the active ones from <MARKET>_STRATEGIES
│   ├── exits.py                      # reusable exit managers (fixed TP + breakeven trail; activation trail)
│   ├── risk.py                       # heat / margin gates shared by the paper and live runners
│   ├── sessions.py                   # trading-session windows per market
│   └── paths.py                      # single source of truth for every filesystem path
│
├── services/                         # External plumbing
│   ├── broker/                       # Upstox client, instrument-master resolver, order manager, feed streamer
│   ├── auth/                         # Upstox OAuth2 + headless auto-login
│   ├── utils/                        # telegram, logger, market_holidays, chart, position_reconciliation
│   └── data/                         # candle fetching helpers
│
├── deploy/systemd/                   # systemd --user units (symlinked from ~/.config/systemd/user/)
│   ├── hft-dryrun.service            # 24/7 paper-trading daemon
│   └── hft-daily-data-topup.service/.timer   # nightly data top-up + DB backup (00:30 IST)
│
├── tests/                            # unit tests
└── var/                              # ALL runtime state (gitignored): archive/<market>/, cache/, logs/, db/
```

---

## Supported Instruments & Trading Profiles

Every symbol below is calibrated with its **own** entry thresholds in `markets/commodity/scalping/backtest.py`'s `ENTRY_THRESHOLDS` dict (mirrored in `engine/live_dryrun.py`) — nothing is shared across symbols by assumption; each was validated independently on real data.

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

### NSE Currency Derivatives (`markets/currency/scalping/backtest.py`, `ENTRY_THRESHOLDS` per pair)

Same feature engine and entry-rule shape as commodities, with pair-specific thresholds calibrated from a 375-combo real-data sweep per pair (2026-09-18) — `strategy.currency_costs` swaps in the genuinely different NCD_FO fee schedule (no STT/CTT at all on currency derivatives; different stamp duty/exchange-fee rates). Session is 09:00–17:00 IST — no MCX-style evening/US-overlap window (a currency pair has no analogous "second session").

| Pair | Status | Real-data result | Sweep robustness |
|---|---|---|---|
| **`USDINR`** | Live | 45 trades, 48.9% win, PF 3.13, +₹13,854 | **258/258 credible (≥15 trade) combos profitable (100%)** — the most robust result in this project |
| **`GBPINR`** | Live | 17 trades, 41.2% win, PF 2.87, +₹4,679 | **192/192 (100%)** |
| **`EURINR`** | Live | 43 trades, 39.5% win, PF 2.23, +₹9,721 | 201/359 (56%) — still a real, majority-robust edge |
| `JPYINR` | Not added | — | 0/375 combos reached the 15-trade credibility bar — youngest contract, not enough real days yet. Not a negative finding, just insufficient data; revisit once its archive grows. |

**Important shape difference from commodities**: all three live pairs win *under 50%* of trades but are solidly profitable (profit factors 2.2–3.8x) — winners run 2-4x bigger than losers, the opposite payoff shape from crude/gold's 65-70%-win-rate/tight-R:R style. Don't judge these by win rate alone.

### NSE Equity: NIFTY50 Intraday Scalping (`markets/equity/scalping/backtest.py`, ONE shared `ENTRY_THRESHOLDS`)

Structurally different from commodity/currency in three ways, all deliberate:

1. **One shared entry-rule set for the entire 49-stock universe, not per-symbol tuning.** The original version of this scalper (removed 2026-09-18) hand-curated a small "best" universe *because* those stocks already backtested well — a circular selection that guarantees an inflated result regardless of whether any real edge exists. The rebuild fixes this by deciding the universe (`markets/equity/universe.py` — all NIFTY50 names except `TATAMOTORS`, which no longer resolves in Upstox's instrument master) *before* any backtest, and applying identical thresholds to every stock. A stock trades often or rarely purely because its own price action does or doesn't clear the bar — nothing is ever pruned after the fact based on how it performed.
2. **Dynamic ADX-scaled trailing exits, no fixed take-profit.** Once a trade proves itself (moves favorably past an activation threshold that itself scales with how strong the trend looked at entry), a trailing stop — recomputed from the *current* bar's ATR every bar, not frozen at entry — manages the rest of the trade. A strong trend can run well past where a fixed target would have capped it.
3. **A hard cap of 3 concurrent open positions across the whole universe.** An earlier, uncapped version of this backtest allowed up to 10 simultaneous positions at 5% risk each — correlated market-wide moves hit many of them at once on the same bad days (worst single day: -₹11,053 across 16 trades, ~11% of capital), nearly wiping the account despite a profit factor above 1 in aggregate. The cap is a real portfolio-concentration constraint, not a backtest artifact.

| | Status | Result |
|---|---|---|
| **Full NIFTY50 (49 names)** | Live (`DRYRUN_INCLUDE_EQUITY=true`) | Validated across **3 independent train/test folds** (2023-09→2024-08, 2024-07→2025-06, 2025-07→2026-09): win rate 64.7–68.0%, profit factor 1.41–1.56, all three net-positive with no exceptions. |

**Why the original version failed, root-caused (not just re-asserted):** beyond the circular universe selection above, its untuned entry rules generated ~7,700 trades over 4 years averaging just ₹4.07 gross profit each — but ₹17.02 in fees each (mostly Upstox's flat ~₹20-per-leg brokerage cap). Every trade had negative expected value after costs, so capital was mathematically guaranteed to decay toward zero over enough trades, confirmed by testing risk-per-trade from 0.5% to 5% and concurrency caps from 3 to 10 — all converged to the same near-total wipeout. The fix wasn't better risk management, it was fewer, higher-conviction trades (`min_ema_slope` raised to 0.22, a much stronger trend-strength requirement) so each trade's edge meaningfully exceeds the flat fee floor.

**ML was tried and deliberately NOT used.** A LightGBM model trained on the pooled 2.64M-row dataset (`ml/train_equity.py`, since removed) shows real signal (ROC-AUC 0.81, ~3x precision lift over the 4.3% base rate) — but adding it as an entry filter made results *worse* on 1 of 3 test folds, including flipping the most recent period from a +₹195k profit to a -₹15k loss. Equity runs rule-based only, same posture as currency.

**Session**: 09:15–15:30 IST (NSE cash hours), entries gated 09:30–15:15, square-off 15:15.

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

Copy `backend/.env.example` to `backend/.env` and fill it in — the template lists every variable the code reads, with the recommended value. Any CLI flag overrides its `.env` value for that run only. **Changing `.env` needs a daemon restart** (`systemctl --user restart hft-dryrun.service`). Two rules when editing: put comments on their own line (a `KEY=   # comment` with an empty value is read as the comment text by python-dotenv), and set the risk / regime / equity variables explicitly, because `engine/live_dryrun.py` and `engine/live_trading.py` have different built-in defaults for some of them.

The daily Upstox access token is **not** in `.env`: it is cached in `var/cache/upstox_token.json` by `services/auth/`.

### Broker Credentials

| Variable | Description |
|---|---|
| `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` | OAuth2 app credentials from [developer.upstox.com](https://developer.upstox.com/). |
| `UPSTOX_REDIRECT_URI` | OAuth2 redirect URL registered with your Upstox app (default `https://127.0.0.1/`). |
| `UPSTOX_USERNAME` / `UPSTOX_PIN` / `UPSTOX_TOTP_SECRET` | Optional — enable `services/auth/upstox_auto_login.py`'s headless daily token refresh (Selenium + TOTP). Leave blank to refresh manually with `python3 -m services.auth.upstox_auth`. **Security:** the TOTP secret plus PIN is permanent full login access to the account. |
| `UPSTOX_ACCESS_TOKEN` | Fallback only, used if `var/cache/upstox_token.json` doesn't exist yet. Leave blank. |

Upstox keeps **one active access token per app**: a login from anywhere else invalidates the token the daemon holds. Never run a second copy of the daemon (or a second systemd scope) against the same credentials — the two would log in over and over and invalidate each other every 15 minutes.

### Shared Trading Parameters

| Variable | Built-in default | Description |
|---|---|---|
| `TRADING_CAPITAL` | `100000.0` | `--capital` default for `backtest`, `dryrun`, `reset-db`. |
| `INTRADAY_LEVERAGE` | `4.0` (CLI) | `--leverage` fallback whenever `BACKTEST_LEVERAGE` / `DRYRUN_LEVERAGE` is blank. |
| `TRADING_DIRECTION` | `both` | `both` / `long-only` / `short-only`. |
| `ALLOW_SHORTS` | `true` | `false` = never open a short, whatever `TRADING_DIRECTION` says. |

### Backtest Defaults (`cli.py backtest`, MCX commodity)

NSE currency and equity are backtested standalone (`python3 -m markets.currency.scalping.backtest`, `python3 -m markets.equity.scalping.backtest`).

| Variable | Built-in default | Description |
|---|---|---|
| `BACKTEST_SYMBOLS` | *(blank)* | Space-separated symbols, e.g. `CRUDEOILM GOLDM`. Blank = default list. |
| `BACKTEST_RISK_PCT` | `5.0` | Risk % per trade. |
| `BACKTEST_LEVERAGE` | *(blank)* | Blank = `INTRADAY_LEVERAGE`. |
| `BACKTEST_FROM` / `BACKTEST_TO` | *(blank)* | `YYYY-MM-DD`. Blank = full archive. |
| `BACKTEST_FULL_SESSION` | `false` | `true` = full MCX session; `false` = evening US-overlap window only. Keep equal to `DRYRUN_FULL_SESSION`. |

### Dry Run (`cli.py dryrun`, the 24/7 daemon)

| Variable | Built-in default | Description |
|---|---|---|
| `DRYRUN_SYMBOLS` | *(blank)* | MCX + currency symbols, e.g. `CRUDEOILM GOLDM SILVER USDINR`. Currency pairs are detected by name. |
| `DRYRUN_INCLUDE_EQUITY` | `false` (dry run) | `true` = also trade the fixed NIFTY50 universe (49 names). Extends `DRYRUN_SYMBOLS` rather than replacing it. |
| `DRYRUN_RISK_PCT` | `5.0` | Risk % per trade. |
| `DRYRUN_LEVERAGE` | *(blank)* | Blank = `INTRADAY_LEVERAGE`. |
| `DRYRUN_INTERVAL` | `30` | Scan interval, seconds. |
| `DRYRUN_FULL_SESSION` | `true` | Full MCX session vs evening-only — see [Session window](#session-window). |

### Per-Market Risk, Leverage and Switches

| Variable | Built-in default | Description |
|---|---|---|
| `COMMODITY_RISK_PCT` / `CURRENCY_RISK_PCT` / `EQUITY_RISK_PCT` | `DRYRUN_RISK_PCT` | Risk % per trade for that market. Per-symbol overrides (e.g. SILVER, CRUDEOILM) live in code: `_SYMBOL_RISK_PCT_OVERRIDE` / `_SYMBOL_LEVERAGE_OVERRIDE` in `engine/live_dryrun.py`. |
| `COMMODITY_LEVERAGE` / `CURRENCY_LEVERAGE` / `EQUITY_LEVERAGE` | resolved `DRYRUN_LEVERAGE` | Leverage for that market. |
| `ENABLE_COMMODITY_TRADING` / `ENABLE_CURRENCY_TRADING` | `true` | `false` = no new entries for that market (open positions are still managed). |
| `ENABLE_EQUITY_TRADING` | follows `DRYRUN_INCLUDE_EQUITY` | Same, for equity. |

### Strategy Switches

| Variable | Built-in default | Description |
|---|---|---|
| `COMMODITY_STRATEGIES` / `CURRENCY_STRATEGIES` / `EQUITY_STRATEGIES` | strategies with `default_enabled` (the scalpers) | Comma-separated names of the strategy folders under `markets/<market>/` that trade, e.g. `EQUITY_STRATEGIES=scalping,swing`. A strategy file that isn't named here never trades. See [Adding a strategy](#adding-a-strategy). |
| `<MARKET>_<STRATEGY>_RISK_PCT` | the market's risk % | Optional per-strategy risk %, e.g. `EQUITY_SWING_RISK_PCT=1.0`. |
| `USE_COMMODITY_REGIME_FILTER` | `false` (dry run) | Daily-return-autocorrelation regime gate (`core/regime.py`). **Effective for CRUDEOILM only** — validated for crude; extending it to GOLDM/SILVER was tested and reverted (hurts SILVER, no benefit for GOLDM/USDINR). The old name `USE_CRUDE_REGIME_FILTER` is still read as a fallback. |
| `USE_EQUITY_REGIME_FILTER` | `false` | Same gate on the NIFTY50 index for all equity entries. Unvalidated. |
| `ENABLE_MEAN_REVERSION` | `true` in code — **`.env` sets `false`** | VWAP/RSI mean-reversion setup for commodity and equity. Off: it failed out-of-sample on crude (TRAIN PF 2.28 → TEST PF 0.96) and had too few trades elsewhere. Thresholds are hardcoded in the `entry_signal` modules, not env vars. |

### Safety Limits and Money Management (dry run daemon)

| Variable | Built-in default | Description |
|---|---|---|
| `MAX_DAILY_LOSS_PCT` | `5.0` | Account-wide daily loss kill switch (% of day-start capital). Halts new entries; open positions are still managed. |
| `MAX_MARKET_DAILY_LOSS_PCT` | `3.0` | Same, per market (commodity / currency / equity): that market pauses, the others keep trading. |
| `MARKET_COOLDOWN_MINUTES` | `60` | Pause length after a per-market breach; 1.5x at 150% of the limit, 2x at 200%+. |
| `MAX_PORTFOLIO_HEAT_PCT` | `8.0` | Total stop-distance risk across **all** open positions, % of capital. Researched range: 4–8% swing, up to ~10% for tight-stop scalpers. |
| `MAX_MARGIN_UTILIZATION_PCT` | `100.0` | Total margin the open positions may commit, % of capital — margin is one shared pool. A new entry is **sized to the margin still free**, not rejected. Sizing is margin-bound (one trade routinely uses 80–100% of capital), so values well below 100 shrink every position; 90 (with a 50 per-market cap) once blocked essentially every entry. |
| `MAX_MARKET_MARGIN_UTILIZATION_PCT` | `100.0` | Same, per market, so one market can't crowd out the others. Override per market with `COMMODITY_MAX_MARGIN_PCT` / `CURRENCY_MAX_MARGIN_PCT` / `EQUITY_MAX_MARGIN_PCT`. |
| `MAX_POSITIONS_PER_SECTOR` | `1` | Max concurrent open equity positions in one sector (`core/sector_correlation.py`). |
| `TOKEN_CHECK_INTERVAL_MIN` | `15` | How often the daemon re-validates its Upstox token; also re-checked immediately on a broker 401. |
| `SPREAD_SAMPLE_INTERVAL_MIN` | `5` | How often live bid/ask is sampled per symbol **while its market is open**, feeding the adaptive slippage model (`var/logs/spread_samples.csv`). |

Drawdown-scaled position sizing (5 / 10 / 15% drawdown → 10 / 25 / 50% smaller size) is always on and has no switch.

### Telegram Alerts (optional)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather). Blank = alerts disabled. |
| `TELEGRAM_CHAT_ID` | Message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find it. |

### Live Trading Gate

| Variable | Default | Description |
|---|---|---|
| `ALLOW_LIVE_TRADING` | `false` | Gate 2 of 3 (Gate 1 is `KILL_SWITCH_ENGAGED` in `engine/safety_gate.py`, Gate 3 is the armed-state file from `cli.py arm-live-trading`). Read `docs/LIVE_TRADING_ARMING.md` in full before touching it. |

---

## Adding a strategy

A strategy is **one class in one file** — `markets/<market>/<name>/strategy.py` exposing `STRATEGY` (`python3 cli.py new-strategy --market equity --name swing` creates the template). You write the decisions (entry, sizing, exit, costs); the paper runner and the real-order runner both pick it up automatically and apply the same kill switches, cooldowns, heat/margin limits, sector cap and drawdown-scaled sizing, place orders with the right product type, persist the position across restarts, and tag every order and trade with the strategy name. Switch it on with `EQUITY_STRATEGIES=scalping,swing` (or the commodity / currency equivalent); a strategy that isn't named there never trades. Overnight/delivery strategies (`intraday = False`, `product = "D"`, `uses_leverage = False`) are supported. Full guide and a template: **[docs/ADDING_A_STRATEGY.md](docs/ADDING_A_STRATEGY.md)**. A swing-strategy research study across all three markets (documented rules, real-data train/test, costs) is in **[docs/SWING_RESEARCH.md](docs/SWING_RESEARCH.md)** — its result: no equity or currency swing rule survives out-of-sample, and one commodity rule (12-month momentum, `markets/commodity/swing`) is added as an unproven, off-by-default paper candidate.

---

## Unified CLI Cheat Sheet

All commands below assume you're in `backend/` with the venv active (or just call `.venv/bin/python3`, which `cli.py` also auto-re-execs into if you run it with the system Python). Every flag shown has an `.env` equivalent from the table above — omit the flag to use whatever's in `.env`.

### 1. `cli.py backtest` — Walk-forward backtest on historical MCX commodity data

```bash
python3 cli.py backtest [--symbols SYM [SYM ...]]
                         [--capital N] [--risk-pct N] [--leverage N]
                         [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                         [--full-session]
```
| Flag | Meaning |
|---|---|
| `--symbols` | Override the default MCX symbol list. NSE currency is backtested standalone via `markets/currency/scalping/backtest.py` (see #6 below) — not wired into this command. |
| `--capital` | Starting capital in ₹. |
| `--risk-pct` | Risk % of capital per trade. |
| `--leverage` | MIS margin leverage multiplier. |
| `--from` / `--to` | Date range filter (`--from-date`/`--to-date` also accepted). |
| `--full-session` | Trade the full 10:00–22:30 IST session instead of just the evening US-overlap window. |

```bash
# Examples
python3 cli.py backtest                                                  # everything from .env
python3 cli.py backtest --symbols CRUDEOILM GOLDM --from 2026-08-17 --to 2026-09-17
```

### 2. `cli.py dryrun` — Live paper-trading (delegates to `engine/live_dryrun.py`)

```bash
python3 cli.py dryrun [--symbols SYM [SYM ...]]
                       [--capital N] [--risk-pct N] [--leverage N]
                       [--interval N] [--direction {both,long,short}]
```
Always trades MCX commodities + NSE currency together in one daemon (`CRUDEOILM`/`GOLDM`/`USDINR`/`EURINR`/`GBPINR` by default) — each symbol's own session window (MCX 09:00–23:30 IST, currency 09:00–17:00 IST) is handled automatically per-symbol inside the daemon.

| Flag | Meaning |
|---|---|
| `--symbols` | Override the default watchlist. |
| `--interval` | Scan interval, seconds. |
| `--direction` | Restrict to `long` or `short` only, or `both`. |

```bash
# Examples
python3 cli.py dryrun                                    # everything from .env — this is what the systemd service runs
python3 cli.py dryrun --interval 30
python3 cli.py dryrun --direction long
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
Arming the state file is only **one** of three independent gates — `engine.safety_gate.KILL_SWITCH_ENGAGED` must also be hand-edited to `False` in source (and redeployed), and `ALLOW_LIVE_TRADING=true` must be set in `.env`. All three must agree before any broker call is allowed to place a real order; a caller requesting `dry_run=True` is always honored regardless of gate state. See `engine/safety_gate.py`.

### 5. `engine/live_dryrun.py` directly (what `cli.py dryrun` calls under the hood)

Useful when you need a flag `cli.py dryrun` doesn't expose yet (`--long-only`, `--db`, `--account`, `--token`):

```bash
python3 -m engine.live_dryrun [--token TOKEN] [--capital N] [--risk-pct N] [--leverage N]
                        [--symbols SYM [SYM ...]] [--db PATH] [--account ID]
                        [--long-only] [--direction {both,long,short}]
                        [--interval N] [--report] [--reset-db]
```
| Flag | Meaning |
|---|---|
| `--token` | Explicit Upstox access token (otherwise loaded from config/cache/`.env`/auto-login, in that order). |
| `--long-only` | Skip all short setups regardless of `--direction`. |
| `--db` / `--account` | Point at a specific SQLite file / account ID — useful for running multiple independent paper accounts side by side (each gets its own [process lock](#safety-features-dry-run)). |
| `--report` | Print the dashboard and save the equity curve chart, then exit — no trading. |
| `--reset-db` | Wipe and reinitialize the DB before starting. |

```bash
# Examples
python3 -m engine.live_dryrun --capital 100000 --risk-pct 4.0 --leverage 5.0 --interval 30
python3 -m engine.live_dryrun --report --account DRYRUN_ACCOUNT
python3 -m engine.live_dryrun --reset-db --capital 100000
```

### 6. Backtest scripts directly (`markets/commodity/scalping/backtest.py`, `markets/currency/scalping/backtest.py`, `markets/equity/scalping/backtest.py`)

Same engines `cli.py backtest` delegates to, callable directly when you want their full native flag set:

```bash
# MCX Commodity Scalper
python3 -m markets.commodity.scalping.backtest --symbols CRUDEOILM --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 -m markets.commodity.scalping.backtest --symbols CRUDEOILM GOLDM --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 -m markets.commodity.scalping.backtest --symbols CRUDEOILM --capital 100000 --risk-pct 10.0 --size-mode risk   # pure risk-budgeted, unconstrained by margin

# NSE Currency Derivatives Scalper
python3 -m markets.currency.scalping.backtest --symbols USDINR --capital 100000 --risk-pct 10.0 --leverage 7.0
python3 -m markets.currency.scalping.backtest --symbols USDINR EURINR GBPINR --capital 100000 --risk-pct 10.0 --leverage 7.0

# NSE Equity Intraday Scalper (full NIFTY50 universe if --symbols omitted)
python3 -m markets.equity.scalping.backtest --capital 100000 --risk-pct 5.0 --leverage 5.0
python3 -m markets.equity.scalping.backtest --symbols RELIANCE TCS HDFCBANK --capital 100000 --risk-pct 5.0 --leverage 5.0
python3 -m markets.equity.scalping.backtest --capital 100000 --risk-pct 5.0 --leverage 5.0 --from 2025-07-01 --to 2026-09-18   # one of the 3 validated OOS folds
```

---

## Running 24/7 as a systemd Service

`engine/live_dryrun.py` is a long-running daemon (it loops across trading days on its own, sleeping through nights/weekends), so it's meant to run under a process supervisor — **not** `nohup`. A `systemd --user` service gives you: survives terminal logout, auto-restarts on crash, centralized logs via `journalctl`, and starts on boot.

### Unit files live in the repo, not just in systemd's directory

All unit files are checked into **`backend/deploy/systemd/`** — not hidden away in `~/.config/systemd/user/` where they'd be invisible to the repo and easy to lose track of. `~/.config/systemd/user/` holds only **symlinks** pointing back into `backend/deploy/systemd/`, so systemd reads the exact file you see and edit in the project — no separate "deploy" step, no copying, no drift between what's committed and what's running.

| Unit | Type | Purpose | Schedule |
|---|---|---|---|
| `hft-dryrun.service` | persistent daemon | Runs `cli.py dryrun` — the 24/7 paper-trading loop | Always on (`Restart=always`) |
| `hft-daily-data-topup.service` + `.timer` | oneshot + timer | Runs `--topup` for every market in one job (`markets/commodity/data.py`, `markets/currency/data.py`, `markets/index_futures/data.py`, `markets/equity/data.py`, plus `engine/backup_db.py`) — one `ExecStart=` each — appending the day's real candles to `var/archive/<market>/<SYMBOL>_<timeframe>.csv`. Timeframes: **1min, 3min, 5min, 15min, 1day** for commodity/currency/index futures; **3min and 5min** for equity. A missing timeframe file is backfilled automatically on the next run | Daily, 00:30 IST |

### First-time setup on a new machine

```bash
cd /var/www/html/hft/backend

# 1. Symlink every unit file from the repo into systemd's user directory
mkdir -p ~/.config/systemd/user
for f in deploy/systemd/*; do
    ln -s "$(pwd)/$f" ~/.config/systemd/user/"$(basename "$f")"
done

# 2. Let user services survive logout/reboot (one-time, machine-wide)
loginctl enable-linger "$USER"

# 3. Load the units and start everything
systemctl --user daemon-reload
systemctl --user enable --now hft-dryrun.service
systemctl --user enable --now hft-daily-data-topup.timer

# 4. Confirm
systemctl --user status hft-dryrun.service --no-pager
systemctl --user list-timers --all --no-pager
```

### Editing a unit

Edit the file directly in `backend/deploy/systemd/` (same as any other project file — visible in your IDE, tracked by git, no secrets in it so safe to commit). Then:

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
systemctl --user restart hft-dryrun.service                # e.g. after editing engine/live_dryrun.py or .env
systemctl --user status hft-dryrun.service --no-pager      # is it running, PID, recent log lines
journalctl --user -u hft-dryrun.service -f                  # follow logs live
journalctl --user -u hft-dryrun.service --no-pager -n 100    # last 100 lines

systemctl --user list-timers --all --no-pager                          # next scheduled fire for every timer
systemctl --user start hft-daily-data-topup.service                     # trigger a data top-up right now (don't wait for 00:30 IST)
journalctl --user -u hft-daily-data-topup.service --no-pager -n 50       # last data top-up's output
```

`Restart=always` on `hft-dryrun.service` is safe with `systemctl stop` — systemd doesn't apply the restart policy to an explicit stop request, only to unexpected exits/crashes.

---

## Safety Features (Dry Run)

Built into `engine/live_dryrun.py`'s `DryRunner`/`main()` — all active by default, no flags needed:

- **Process lock** — refuses to start a second dry-run process for the same `--account`, so a forgotten stray process (or a re-run before the old one exited) can't double-trade the same account. Lock file: `data/.<ACCOUNT_ID>.lock`.
- **Daily-loss kill switch** (`MAX_DAILY_LOSS_PCT`) — halts *new* entries for the rest of the day once realized loss hits the configured % of the day's starting capital. Open positions still get managed/exited normally. Sends a Telegram alert once when tripped, resets automatically the next trading day.
- **Token refresh loop** (`TOKEN_CHECK_INTERVAL_MIN`) — proactively re-validates the Upstox token on a timer, or immediately if the broker's 401 circuit breaker trips. Auto-refreshes via headless login if `UPSTOX_USERNAME`/`PIN`/`TOTP_SECRET` are configured; otherwise alerts via Telegram that a manual `python3 -m auth.upstox_auth` is needed.
- **Graceful shutdown** — both Ctrl-C and `systemctl stop` (SIGTERM) trigger a clean exit with a Telegram alert, not an unhandled crash. The SIGTERM handler is one-shot (re-arms to `SIG_IGN` after the first signal) so a second signal arriving mid-shutdown can't inject a second async exception into the cleanup path.
- **Real, auto-updating holiday calendar** (`services/utils/market_holidays.py`) — the overnight day-rollover loop used to only skip Sunday (an admitted gap in an earlier version of its own comment); it now also skips Saturday and every real NSE/CDS/MCX trading holiday, sourced live from Upstox's own public holiday API (`GET /v2/market/holidays`, no auth needed) rather than a hand-maintained list. Never hardcodes a year — always reflects whatever year it currently is, cached and refetched automatically once a day (and across a year boundary).
- **Layered live-trading kill switch** (`engine/safety_gate.py`) — see [`cli.py arm-live-trading`](#4-cliparm-live-trading--disarm-live-trading--the-layered-kill-switch) above.

> **Note on scope**: this is a paper-trading system end to end — `UpstoxBroker` is always constructed with `dry_run=True` (enforced independently by `engine/safety_gate.py` even if a caller ever requested otherwise), and no code path currently places real orders. These safety features harden the *paper* daemon (crash alerting, daily-loss discipline, token hygiene); they are prerequisites for eventually going live, not a live-trading switch.

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

The equity curve (`services/utils/chart.py`) is a matplotlib line chart built from `engine/database.py`'s `portfolio_snapshots` table (one row recorded per trade close), aggregated to **one point per calendar day** (that day's end-of-day capital) spanning from the first trading day through today — not a noisy per-trade intraday chart. It's regenerated and sent to Telegram at the end of every trading day, and also saved to `logs/equity_<ACCOUNT_ID>.png` on every `--report` call:

```bash
python3 -m engine.live_dryrun --report --commodity --account DRYRUN_ACCOUNT
# -> prints the dashboard AND saves var/logs/equity_DRYRUN_ACCOUNT.png
```

---

## Quantitative Strategy & Autocorrelation Regime Architecture

The trading framework operates as a **100% pure rule-based quantitative engine** coupled with statistical market regime filters:

### 1. High-Conviction Trend Expansion Core
Trade entry signals are determined via multi-factor technical and microstructure criteria across 5-minute candles:
- **ADX & DMI Trend Strength**: Verifies trend momentum (`adx >= min_adx` and `+DMI > -DMI` for longs).
- **EMA Trend Slope**: Filters for positive directional velocity (`ema_slope >= min_ema_slope`).
- **Opening Range Breakout (ORB)**: Evaluates expansion relative to opening candle ranges.
- **VWAP Alignment**: Confirms execution direction relative to institutional volume-weighted average price.
- **Volume Surge Ratio**: Requires volume expansion relative to rolling average volume.

### 2. Multi-Symbol Autocorrelation Market Regime Filter (`core/regime.py`)
Rather than relying on black-box ML models (which overfit and reduce profits during volatile chop), the system utilizes an institutional daily-return autocorrelation regime filter:
- Computes rolling lag-1 autocorrelation ($\rho_1$) on daily log returns.
- **Persistent Trend Regime ($\rho_1 > 0$)**: Trend breakouts continue with high follow-through; signals execute normally.
- **Mean-Reverting / Choppy Regime ($\rho_1 \le 0$)**: Trend breakouts experience high failure rates; the regime gate dynamically blocks new entries for that symbol/segment, avoiding false breakouts and preserving capital.

### 3. Dynamic Position Sizing & Anti-Martingale Scaling
- **Segment-Specific Risk & Leverage**: Calibrated independently for Commodities (`4.0%` risk / `5.0x` leverage), Currencies (`4.0%` risk / `5.0x` leverage), and Equities (`4.0%` risk / `5.0x` leverage).
- **Drawdown-Scaled Sizing (Anti-Martingale)**: Automatically reduces position size if trailing account capital experiences a drawdown (5%/10%/15% drawdown scales risk down to 90%/75%/50%), restoring automatically as capital rebounds.
- **Portfolio Heat Cap (`MAX_PORTFOLIO_HEAT_PCT=12.0%`)**: Limits aggregate simultaneous market risk across all open positions.

---

## Real MCX Data via Upstox

`var/archive/commodity/*.csv` is populated from **genuine historical MCX candles**, fetched via `markets/commodity/data.py` using the same Upstox broker/account this bot already live-trades through — no separate data vendor or credentials needed. `markets/commodity/synthetic_data.py`'s earlier random-walk generator is no longer used for training or backtesting (see git history 2026-09-10 for why: everything validated against it — win rates, parameter tuning — was fit to synthetic patterns, not real market behavior).

**Hard constraint**: MCX commodity futures are monthly-expiry contracts, not continuously-listed instruments. Upstox's real history for the *current* active contract only reaches back to that contract's own listing date — typically ~1 month, not years. Requesting further back returns zero candles, not a clipped result. Real history accumulates one genuine trading day at a time via the daily top-up job; there's no way to get more than ~1 month at once without a paid data vendor (TrueData, Global Data Feeds, PortaraCQG all carry real MCX intraday history, but pricing is quote-based, not self-serve). **Every backtest result quoted in this README reflects this real, currently ~32-day, window** — `markets/commodity/scalping/backtest.py` prints the actual archive date range it used on every run (never a hardcoded/stale label) specifically so this can't be silently misrepresented.

**Four intervals maintained per symbol**: 1-minute, 5-minute (the one the strategy/ML model actually consumes), 15-minute, and 1-day (which Upstox retains for noticeably longer than intraday — often several months back even when intraday is capped at ~1 month).

```bash
python3 -m markets.commodity.data           # full initial backfill, all tracked symbols, all 4 intervals
python3 -m markets.commodity.data --topup     # incremental: fetch only candles newer than what's archived (what the daily timer runs)
```

Symbols covered: `CRUDEOILM`/`CRUDEOIL`, `GOLDM`/`GOLD`, `SILVERMIC`/`SILVER`, `COPPER` (base-symbol and mini-contract archive files are kept aligned — `train_commodity.py`/`markets/commodity/scalping/backtest.py` look up whichever name they're given via an alias map).

**NSE currency derivatives** (`var/archive/currency/*.csv`, via `markets/currency/data.py`) hit the same real-data wall — same ~1-month-per-contract cap, verified the same way (a wide single-call request to Upstox's history API was found to silently truncate instead of erroring; `markets/currency/data.py` fetches in small chunks and unions the results rather than trusting one wide call). No free third-party dataset fills this gap either — checked GitHub and Kaggle directly (2026-09-18): `ShabbirHasan1/NSE-Data` has no currency segment at all, and `jugaad-data`'s official-NSE-bhavcopy library doesn't cover currency derivatives in its roadmap either. Real data here, same as MCX, only grows one real day at a time via the daily top-up job.

```bash
python3 -m markets.currency.data           # full initial backfill, all 4 pairs, all 4 intervals
python3 -m markets.currency.data --topup     # incremental (what the daily timer runs)
```

**NSE equity** (`var/archive/equity/*.csv`, via `markets/equity/data.py`) does NOT hit the monthly-expiry wall above — NSE cash equities are continuously-listed, not futures/derivatives contracts, so real history goes back to each stock's own genuine listing/data-availability date. Confirmed directly: ~76,500 real 5-minute candles per symbol, 2022-08-01 through today, for all 49 NIFTY50 names. Uses the same shared `fetch_real_history_backward` chunked-fetch utility as commodity/currency (a hard per-chunk timeout with retry-then-gap logic was added here specifically — a genuine network hang was found and fixed while building this downloader; a timed-out chunk is now retried and, if still stuck, left as an honest gap rather than being misread as "end of history" and silently truncating everything older).

```bash
python3 -m markets.equity.data              # full initial backfill, all 49 NIFTY50 symbols
python3 -m markets.equity.data --topup        # incremental (what the daily timer runs)
python3 -m markets.equity.data --symbols RELIANCE TCS   # subset
```

---

## Statutory Taxation & Friction Schedule

| Cost Head | MCX Futures (`CRUDEOILM`) | NSE Currency Derivatives | NSE Equity (Intraday MIS) |
|---|---|---|---|
| **CTT / STT** | **0.010%** on Sell turnover | **None — exempt** | **0.025%** on Sell turnover |
| **Brokerage** | **₹20 flat cap** per order leg | **₹20 flat cap** per order leg | **₹20 flat cap** per order leg |
| **Stamp Duty** | **0.002%** on Buy turnover | **0.0001%** (₹10/crore) on Buy turnover | **0.003%** on Buy turnover |
| **Exchange Turnover** | **0.0021%** on total turnover | **0.0009%** on total turnover | **0.00325%** on total turnover |
| **SEBI Regulatory Fee** | **₹10 per Crore** (0.0001%) | **₹10 per Crore** (0.0001%) | **₹10 per Crore** (0.0001%) |
| **GST** | **18%** on (Brokerage + Exch + SEBI) | **18%** on (Brokerage + Exch + SEBI) | **18%** on (Brokerage + Exch + SEBI) |
| **Slippage Buffer** | **½-tick per leg** | **½-tick per leg** | **½-tick per leg** |

Currency derivatives carry the lightest friction of the three — no STT/CTT at all, and a much lower stamp duty (reduced from ₹200/crore to ₹10/crore specifically for currency & interest-rate derivatives). Equity carries the heaviest STT (0.025% vs. commodity's 0.010%) — this is exactly why the original equity scalper's high-frequency/small-edge approach failed (see [NSE Equity](#nse-equity-nifty50-intraday-scalping-backtest_equitypy-one-shared-entry_thresholds)): the ₹20 flat brokerage cap alone exceeded the average trade's entire gross edge at that trade frequency.

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

Same capital/risk/leverage as above; `markets/currency/scalping/backtest.py` (rule-based).

| Pair | Trades | Win Rate | Profit Factor | Net Realized | Max Drawdown |
|---|---|---|---|---|---|
| `USDINR` | 45 | 48.9% | 3.13 | **+₹13,853.68 (+13.85%)** | -1.46% |
| `EURINR` | 43 | 39.5% | 2.23 | **+₹9,721.30 (+9.72%)** | -3.85% |
| `GBPINR` | 17 | 41.2% | 2.87 | **+₹4,678.85 (+4.68%)** | -1.80% |

Note the win rates: all under 50%, yet all profitable with strong profit factors — see [NSE Currency Derivatives](#nse-currency-derivatives-backtest_currencypy-entry_thresholds-per-pair) above for why. `USDINR`/`GBPINR` had every one of their credible sweep combinations profitable (100%); `EURINR` had 56%. Same small-sample caveat as commodities applies.

### NSE Equity — full NIFTY50 universe (2022-08 to 2026-09, 3 independent train/test folds)

Unlike commodity/currency, equity has a genuine multi-year real archive (NSE cash has no monthly-expiry cap), so this was validated with real out-of-sample folds rather than one short window. Capital ₹100,000, risk 5%, leverage 5x, max 3 concurrent positions, `markets/equity/scalping/backtest.py`, no ML filter (see [Equity's ML model](#equitys-ml-model-trained-but-deliberately-not-used-live) for why).

| Fold | Test period | Trades | Win Rate | Profit Factor | Net Realized |
|---|---|---|---|---|---|
| 1 | 2025-07-01 to 2026-09-18 | 422 | 64.7% | 1.43 | **+₹195,313 (+195.31%)** |
| 2 | 2024-07-01 to 2025-06-30 | 453 | 68.0% | 1.56 | **+₹345,530 (+345.53%)** |
| 3 | 2023-09-01 to 2024-08-31 | 599 | 65.8% | 1.41 | **+₹236,449 (+236.45%)** |

All three folds are independently seeded at ₹100,000 (not chained/compounded across folds) specifically so each fold's win rate/PF is a fair, comparable measurement — win rate holds in a tight 64.7–68.0% band and profit factor in 1.41–1.56 across three non-overlapping multi-month periods, the strongest cross-period consistency found anywhere in this project. Max drawdown is real and significant on all three (33–43%) — this is a genuinely rougher ride than the commodity/currency numbers above, consistent with equity's higher-beta, high-conviction-but-fewer-trades shape (see [NSE Equity](#nse-equity-nifty50-intraday-scalping-backtest_equitypy-one-shared-entry_thresholds) for the concentration-risk cap that keeps it from being worse). Not yet proven with real capital — currently paper-trading only.

---

## Automated Testing Suite

To run all unit tests (statutory cost calculators, the scalper pipelines, the strategy framework and the risk gates):

```bash
cd backend
.venv/bin/python3 -m unittest discover -s tests
RUN_REPLAY=1 .venv/bin/python3 -m unittest tests.test_strategy_parity   # + the ~1 min bar-by-bar replay of the paper runner
```

`tests/test_strategy_parity.py` locks the trading behaviour: golden files record what the scalpers did (orders, trades, real-order broker calls) and the code must reproduce them exactly. If you change behaviour **on purpose**, regenerate the golden that moved (commands are in that file's docstring).
