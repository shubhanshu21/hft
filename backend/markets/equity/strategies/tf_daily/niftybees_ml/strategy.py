"""equity / niftybees_ml -- hold NIFTYBEES (the Nifty 50 ETF) unless a machine-learning forecast of the next 20 trading days turns negative.

Model (added 2026-10-14, user: "add this strategy with ML and niftybees i want to experiment it more"):
  * features: 25 daily values from the nightly archives (features.py): ETF returns / RSI / distance from its 20-50-200 EMAs / fall from the 52-week
    high, NIFTY 50 breadth (50 / 200-day EMA, 20-day highs, advance-decline), FII long share of index futures (NSE participant OI), Nifty opening
    gap / last hour / range, Bank Nifty vs Nifty, India VIX;
  * target: the ETF's return from the next open to the open 20 trading days later;
  * model: 1,000 random Fourier features + ridge regression (the "complex model" idea of Kelly, Malamud & Zhou 2024), averaged over SEEDS
    random draws; retrained at the start of every month on all rows whose 20-day outcome was already known (no look-ahead);
  * decision once a week on Friday's close: hold the ETF while the forecast is above 0, sell when it is not.
Size: at most SHARE of the account (NIFTYBEES_ML_SHARE, default all of it). Safety stop SAFETY_STOP below the buy.
Sharing the Rs1 lakh paper account with trend_ema_cross at 1/3 HURT the account (2023-01 -> 2026-10-13: Rs2,49,558 alone -> Rs2,17,375 together;
2026 +12.2% -> +3.5%): the ETF holds a slot ~95% of the time and starves the stock trades. Not enabled in paper trading for that reason.
Walk-forward test (NIFTYBEES alone, all-in, Rs1 lakh, 2023-01 -> 2026-10-13, real Upstox delivery fees): buy and hold Rs1,29,516 (2026 -12.2%);
this 8-draw version through core/backtester (sells on Friday's close): Rs1,28,207 (2023 +13.6%, 2024 +10.4%, 2025 +11.6%, 2026 -8.4%); single random draws Rs1,29,319 to Rs1,38,444 (7 of 8 beat buy and hold; 2026 -7.5% to -10.9%). A small edge (about +1.5%/yr) that still
loses in a falling year: an experiment, not a proven strategy.

Paper trading (live runner):
  * decides on the first scan of the entry window (09:15-10:00) after a Friday, using data up to that Friday (the nightly archive), so it trades at
    about the next open, as in the backtest; the backtester calls at midnight of each day and acts on Fridays;
  * the runner does not watch a strategy's stop, so manage() checks the safety stop on today's price.
"""
from __future__ import annotations

import os
from datetime import datetime, time
from functools import lru_cache

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs

from .features import ETF, etf_daily, features

HORIZON = 20                     # trading days the model forecasts
N_FEATURES, GAMMA, ALPHA = 1000, 0.5, 10.0
SEEDS = tuple(range(8))
SHARE = float(os.environ.get("NIFTYBEES_ML_SHARE", "1.0"))   # largest share of the account in the ETF (1.0 = all of it; 0.33 when sharing an account)
SAFETY_STOP = 0.20               # crash guard only; normal exits are the weekly forecast
ENTRY_WINDOW = (time(9, 15), time(10, 0))


def _labels() -> pd.Series:
    o = etf_daily()["open"]
    return o.shift(-(HORIZON + 1)) / o.shift(-1) - 1


@lru_cache(maxsize=64)
def _models(month: str, stamp: str):
    """Scaler + one ridge per seed, trained on rows whose label was known before `month` started."""
    X = features()
    y = _labels().reindex(X.index)
    before = X.index[X.index < pd.Timestamp(month + "-01")]
    if len(before) <= HORIZON + 60:
        return None
    cut = before[-(HORIZON + 2)]                                   # the last row whose 20-day outcome was complete before the month began
    train = X.index[(X.index <= cut) & y.notna().values]
    sc = StandardScaler().fit(X.loc[train])
    z = sc.transform(X.loc[train])
    out = []
    for seed in SEEDS:
        rng = np.random.default_rng(seed)
        w, b = rng.normal(size=(X.shape[1], N_FEATURES)), rng.uniform(0, 2 * np.pi, N_FEATURES)
        phi = np.sqrt(2 / N_FEATURES) * np.cos(z @ w * GAMMA + b)
        out.append((w, b, Ridge(alpha=ALPHA).fit(phi, y.loc[train])))
    return sc, out


def forecast(day: pd.Timestamp) -> float | None:
    """Average 20-day return forecast made on `day`'s close (None when there is not enough history or no feature row for that day)."""
    X = features(day.strftime("%Y-%m-%d"))
    if X.empty or X.index[-1] != day:
        return None
    m = _models(day.strftime("%Y-%m"), str(features().index[-1].date()))
    if m is None:
        return None
    sc, models = m
    z = sc.transform(X.iloc[[-1]])
    return float(np.mean([r.predict(np.sqrt(2 / N_FEATURES) * np.cos(z @ w * GAMMA + b))[0] for w, b, r in models]))


def decision_day(now: datetime) -> pd.Timestamp | None:
    """The Friday whose close this call acts on: the backtester calls at midnight of each day (act on Fridays); live acts on the first day after
    a Friday (the newest archived day before today is that Friday)."""
    if (now.hour, now.minute) == (0, 0):
        t = pd.Timestamp(now.date())
        return t if t.weekday() == 4 else None
    last = features().index
    last = last[last < pd.Timestamp(now.date())]
    if len(last) and last[-1].weekday() == 4 and (pd.Timestamp(now.date()) - last[-1]).days <= 4:
        return last[-1]
    return None


class NiftyBeesML(Strategy):
    name = "niftybees_ml"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 30
    max_positions = 1
    sector_cap = False
    id_prefix = "NBM"
    universe = (ETF,)                # the web backtester offers only this symbol

    def trades(self, symbol: str) -> bool:
        return symbol.upper() == ETF

    def due(self, now: datetime) -> bool:
        return now.weekday() < 5 and ENTRY_WINDOW[0] <= now.time() <= ENTRY_WINDOW[1]

    def entry(self, ctx: EntryContext) -> Signal | None:
        day = decision_day(ctx.now)
        if day is None or not ctx.candles:
            return None
        f = forecast(day)
        if f is None or f <= 0:
            return None
        price = float(ctx.candles[-1]["close"])
        account = ctx.account_equity or ctx.capital
        qty = int(min(account * SHARE, ctx.capital) // price)
        if qty < 1:
            return None
        stop = price * (1 - SAFETY_STOP)
        return Signal(symbol=ctx.symbol, direction="long", entry_price=price, stop_loss=stop, qty=qty, stop_dist=price - stop,
                      setup_type=f"ml forecast +{f * 100:.1f}% (20d)")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles:
            return None
        today = ctx.candles[-1]
        live = (ctx.now.hour, ctx.now.minute) != (0, 0)
        if live and float(today["low"]) <= pos["current_stop"]:
            return ExitDecision(min(pos["current_stop"], float(today["open"])), "initial_stop")
        day = decision_day(ctx.now)
        if day is None or (live and not self.due(ctx.now)):
            return None
        f = forecast(day)
        if f is not None and f <= 0:
            return ExitDecision(float(today["close"]), "ml_forecast_down")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = NiftyBeesML()
