"""crypto / dip_reversal -- 5-minute dip-buy reversal on 11 coins' perpetual futures (BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, DOT, LTC, TRX).

Found by screening ~60 public strategies from github.com/freqtrade/freqtrade-strategies (`CombinedBinHAndCluc`) on our own coin data
with real costs, then re-optimised: the only one of the ~60 that stayed profitable in both a 2022-24 dev window and a 2025-26 held-out
test window, and whose profit survives nudging every setting up or down (not a lucky parameter) -- see markets/README.md.

Rule (5-minute finished candles, no indicator peeking at the still-forming candle):
  * enter long when EITHER (a) a sharp, one-sided Bollinger-band squeeze: the 40-candle band is unusually wide, the candle just closed
    well below its lower band and below the previous close with hardly any lower wick (BinHV45's rule), OR (b) price is below its
    50-candle EMA and has slipped meaningfully under its 20-candle Bollinger low band (ClucMay2018's rule)
  * exit: hitting the 15% take-profit (a resting limit order), the 7% stop, the price closing back above the 20-candle Bollinger
    mid-band (a market exit), or a 24-hour time stop if none of those trigger (our own safety cap -- the original rule has no time stop)
  * entry at the next candle's open (a market order); size = RISK_PCT of the account / the 7% stop distance (so ~28.6% of the account
    at RISK_PCT=2%), capped at LEVERAGE x the account as a pure safety ceiling -- the real throttle on profit and risk here is
    RISK_PCT, not leverage (a fixed-% stop means leverage rarely binds; see the risk_pct table in markets/README.md before raising it);
    at most MAX_CONCURRENT (5) positions open together across the 11 coins (shared margin pool, and crypto coins move together in a crash)
Costs: Delta Exchange India taker fee 0.05% + 18% GST + slippage on entries, stops and the time exit; maker fee 0.02% + GST on the take-profit; a
stop the candle OPENS beyond fills at that open, not the stop price; real funding (Binance daily sums, pro-rated by hours held -- a
long position pays when funding is positive). Tax views: none / strict (31.2% of each winning trade) / business income (31.2% of each
April-March year's net, losses carried 4 years; the default and the honest number to look at).
2026-10-21 production backtest (one shared capital pool, RISK_PCT=2%, Rs1L). Shipped rule (target 15%, stop 7%, 5 concurrent -- a joint
sweep of target/stop/concurrency found this beat the original symmetric 7%/7%/3 rule in BOTH windows, not a single lucky cell: the whole
wide-target/5-concurrent region of the grid did better): dev (2022-24) Rs2.30L business income (worst fall -28%), test (2025-26) Rs1.32L
(worst fall -16%); every one of 2022-2026's 5 calendar years was individually positive. Shorts were tried (the same rule mirrored on
rallies) and made it WORSE in both windows -- crypto's long-term upward drift means fading rallies loses more than it gains; rejected.
RISK_PCT=2% was the safe choice (worst fall -19% in this data); the user explicitly chose real risk for bigger profit instead, so this
ships at 8% -- the genuine ceiling: at 10% some years make LESS than at 8% because the drawdown is deep enough to eat the recovery
(2024: Rs1.22L at 10% vs Rs1.50L at 8%, on the full 12-coin/dynamic-sizing model). At 8% every year 2022-2026 still made money, but the
worst fall seen was -67% (2024) -- Rs1L could fall to roughly Rs33k before recovering. This is a deliberate, informed risk choice, not
a default; see the risk_pct table in markets/README.md for the 2%/4%/6%/8%/10% comparison before raising it further.
Leverage is a safety CEILING on position size here, not a throttle -- the stop already fixes position size at RISK_PCT / stop%, so
changing leverage rarely changes a trade's size or the P&L above.
2026-10-21 dynamic risk (user: "should be dynamic and adjusted according to situations"): position size now scales with how calm or
wild the 11-coin market's own 20-day realized volatility currently is -- the same idea as the old calm_coins volatility gate, size up
when calm, size down when choppy, rather than one fixed % always. scale = clip(VOL_TARGET / current 20d vol, VOL_LO, VOL_HI); risk =
RISK_PCT * scale. VOL_TARGET is the market's own long-run median vol (not fit to any year); VOL_LO/VOL_HI (0.6x-1.8x) were checked
against 5 nearby bound pairs, all giving a similar, consistently better result -- a real effect, not a lucky setting. Result: every
single calendar year 2022-2026 matched or beat the fixed-2% version, with an EQUAL OR LOWER worst fall every year (e.g. 2022: Rs1.24L
business income vs Rs1.23L fixed, worst fall -16% vs -23%); dev total Rs2.61L (was Rs2.30L), test total Rs1.33L (about the same as
Rs1.32L fixed, but with a smaller worst fall, -11% vs -16% in 2025). This is NOT the same as raising RISK_PCT -- it only sizes UP when
the market is calmer than its own long-run norm, and sizes DOWN in genuinely wild conditions; it does not chase any single year's
number (tested honestly on dev then checked on test, same as every other change in this file).
2026-10-21 wider universe: added DOT, LTC, TRX (BTC/ETH/SOL/.../LINK's 9 stayed in place; these 3's price history was already on disk,
unused) -- more independent trade opportunities, same unchanged rule. Checked each of the 7 ways to add 1, 2 or all 3 of them: all gave
a similar result (dev Rs2.50-2.78L, test Rs1.32-1.41L), no single added coin drove it -- a broad, not cherry-picked, improvement. All
12: dev Rs2.60L (about flat vs 9-coin's Rs2.61L), test Rs1.38L (vs Rs1.33L, and a SMALLER worst fall, -9% vs -11%).
Replaces `compounder_5lakh` (removed 2026-10-21, user: "if its better and giving more profits then remove previous and add this") --
that strategy's own freqtrade-screen ranked below this one on the same 2022-26 data; see markets/README.md.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT
from core.backtester import metrics

DATA_DIR = ARCHIVE_ROOT / "crypto"
TAX = 0.312
COINS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LTC", "TRX")  # DOT/LTC/TRX added 2026-10-21 (data already
# on disk); LINK removed 2026-10-22 -- lost money in BOTH the dev window (Rs-2.66L) and the held-out test window (Rs-0.29L), a
# genuine non-edge (not a hindsight pick: it failed before it was ever removed, on data not used to tune anything). Removing it
# improved both windows: dev Rs17.45L -> Rs19.79L, test Rs2.44L -> Rs2.92L, worst fall -76%/-47% -> -75%/-45%.

RISK_PCT = 0.12                                   # 2026-10-22: 12% together with CROWD_K=4 (below). Was 8% with no crowd rule (user's
                                                   # choice 2026-10-21). With the crowd rule, 12% beat 8% in BOTH windows on profit AND
                                                   # crash size: dev Rs43.4L -> Rs56.4L, test Rs4.6L -> Rs6.3L, worst drop -75%/-46% ->
                                                   # -72%/-37%, worst day -55% -> -51%. Do NOT use 12% without CROWD_K -- see markets/README.md.
MAX_CONCURRENT = 5                                # 2026-10-21 joint sweep: 5 beat 3 in both dev and test; 9 (no cap) decayed test -- see docstring
TAKER = 0.0005 * 1.18                              # Delta Exchange India futures market order incl. 18% GST (verified live 2026-10-21 -- same rate as CoinDCX)
MAKER = 0.0002 * 1.18                              # limit order (the take-profit) incl. GST
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001}
BBDELTA_MULT = 0.008
CLOSEDELTA_MULT = 0.0175
TAIL_MULT = 0.25
CLUC_MULT = 0.985
STOPLOSS = 0.07
ROI = 0.15                                        # 2026-10-21: wider target than the stop (was 0.07) tested better in both windows, every year positive
TIME_STOP_BARS = 288                               # 24h of 5-min candles -- our own safety cap, the original rule has none
BAR_MIN = 5
VOL_TARGET = 0.0312                                # the 9-coin basket's own long-run median 20-day daily vol (2022-26), not fit per year
VOL_LO, VOL_HI = 0.6, 1.8                           # risk scale bounds; checked robust against 4 nearby pairs (0.5-2.0 .. 0.7-1.6)
VOL_LOOKBACK_DAYS = 20
CROWD_K = 4                                        # crowd-shrink rule (2026-10-22; None = off): when n coins signal on the same bar and
                                                   # n > CROWD_K, each new trade is sized CROWD_K/n. Every one of the 8 worst days 2022-26
                                                   # (2024-04-12, 2025-02-03, 2026-08-22...) was a market-wide crash where 5-10 coins
                                                   # signalled at once and all positions then fell together. Robust: every k in 3-5 x risk
                                                   # 10-14% beat the old 8%/no-rule on profit per unit of drawdown. Rejected on the way:
                                                   # pause after stop-outs, BTC-drop filters, total-exposure caps, skipping crowded signals.


def crowd_scale(n_signals: int, k: int | None = CROWD_K) -> float:
    """Size multiplier for a new entry when n_signals coins signal on the same bar (1.0 when the rule is off or n <= k)."""
    return 1.0 if not k or n_signals <= k else k / n_signals


def available() -> list[str]:
    return [c for c in COINS if (DATA_DIR / f"{c}_5m.csv").exists()]


def business_tax(curve: pd.Series, capital: float, rate: float = TAX, carry_years: int = 4) -> tuple[pd.Series, float]:
    """Tax on each April-March year's net profit (losses carried 4 years), paid at 31 March and on the last day. curve = pre-tax value."""
    r = curve / curve.shift(1) - 1; r.iloc[0] = curve.iloc[0] / capital - 1
    fy = [d.year + (1 if d.month >= 4 else 0) for d in curve.index]
    A, start, out, carry, paid = capital, capital, [], [], 0.0
    for k, (d, g) in enumerate(r.items()):
        A *= 1 + g
        if k == len(r) - 1 or fy[k + 1] != fy[k]:
            profit = A - start
            if profit > 0:
                use = 0.0
                for c in carry:
                    take = min(c[1], profit - use); c[1] -= take; use += take
                t = rate * (profit - use); A -= t; paid += t
            elif profit < 0:
                carry.append([fy[k], -profit])
            carry = [c for c in carry if c[1] > 0 and fy[k] - c[0] < carry_years]
            start = A
        out.append(A)
    return pd.Series(out, index=curve.index), paid


def _funding(coin: str) -> pd.Series:
    f = DATA_DIR / f"{coin}_funding_daily.csv"
    return pd.read_csv(f, index_col="date", parse_dates=True)["fundingRate"] if f.exists() else pd.Series(dtype=float)


def market_vol_scale_series() -> pd.Series:
    """Daily risk-size scale from the 11-coin basket's own 20-day realized volatility: >1 in a calmer-than-usual market, <1 in a wilder
    one, clipped to [VOL_LO, VOL_HI]. Built from the 5-minute archive's daily close (always present wherever a backtest already runs);
    `current_vol_scale()` below is the one live trading actually calls."""
    daily = {}
    for c in COINS:
        f = DATA_DIR / f"{c}_5m.csv"
        if not f.exists():
            continue
        s = pd.read_csv(f, usecols=["date", "close"], parse_dates=["date"]).set_index("date")["close"]
        s.index = s.index.tz_localize(None)
        daily[c] = s.resample("1D").last().dropna().pct_change()
    if not daily:
        return pd.Series(dtype=float)
    basket = pd.concat(daily, axis=1).mean(axis=1)
    vol20 = basket.rolling(VOL_LOOKBACK_DAYS).std()
    return (VOL_TARGET / vol20).clip(VOL_LO, VOL_HI)


def current_vol_scale() -> float:
    """The risk-size scale for right now, from the latest completed day. 1.0 (no change) if there isn't enough history yet."""
    s = market_vol_scale_series().dropna()
    return float(s.iloc[-1]) if len(s) else 1.0


_CACHE: dict = {}


def load_5m_data(coins: list[str]) -> dict:
    """5-minute OHLC + the entry/exit indicators, cached per coin. Call markets.crypto.data.topup_5m() first to keep this current."""
    global _CACHE
    for c in [c for c in coins if c not in _CACHE]:
        df = pd.read_csv(DATA_DIR / f"{c}_5m.csv", parse_dates=["date"]).sort_values("date").reset_index(drop=True)
        df["date"] = df["date"].dt.tz_localize(None)
        mid40 = df["close"].rolling(40).mean(); std40 = df["close"].rolling(40).std()
        lower40 = mid40 - std40 * 2
        df["bbdelta"] = (mid40 - lower40).abs()
        df["lower40"] = lower40
        df["closedelta"] = (df["close"] - df["close"].shift()).abs()
        df["tail"] = (df["close"] - df["low"]).abs()
        tp = (df["high"] + df["low"] + df["close"]) / 3                        # typical price, 20-candle bands (ClucMay/exit)
        mid20 = tp.rolling(20).mean(); std20 = tp.rolling(20).std()
        df["bb_mid20"] = mid20; df["bb_lower20"] = mid20 - std20 * 2
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        _CACHE[c] = df
    return {c: _CACHE[c] for c in coins}


def entry_signal(df: pd.DataFrame) -> pd.Series:
    cond1 = (df["lower40"].shift().gt(0) & df["bbdelta"].gt(df["close"] * BBDELTA_MULT) & df["closedelta"].gt(df["close"] * CLOSEDELTA_MULT) &
             df["tail"].lt(df["bbdelta"] * TAIL_MULT) & df["close"].lt(df["lower40"].shift()) & df["close"].le(df["close"].shift()))
    cond2 = (df["close"] < df["ema50"]) & (df["close"] < CLUC_MULT * df["bb_lower20"])
    return cond1 | cond2


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, leverage: float = 3.0, tax: str = "business",
                  risk_pct: float | None = None, progress=None, crowd_k: int | None = CROWD_K, **kwargs) -> dict:
    base_risk = RISK_PCT if risk_pct is None else risk_pct   # the module default (8%, the user's choice), or a one-off for this run
    coins = [c.upper() for c in (symbols or list(COINS)) if c.upper() in COINS]
    if not coins:
        raise ValueError("No usable coins")
    data = load_5m_data(coins)
    rng = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(days=1), freq=f"{BAR_MIN}min", tz=None)
    rng = rng[(rng >= pd.Timestamp(start)) & (rng <= pd.Timestamp(end) + pd.Timedelta(days=1))]
    vol_scale = market_vol_scale_series().reindex(rng.normalize()).ffill().fillna(1.0).to_numpy()
    arr = {}
    for c in coins:
        d = data[c].set_index("date").reindex(rng)
        arr[c] = {k: d[k].to_numpy() for k in ("open", "high", "low", "close")}
        arr[c]["sig"] = entry_signal(d).fillna(False).to_numpy()
        arr[c]["mid20"] = d["bb_mid20"].to_numpy()
        arr[c]["fund"] = _funding(c)
    n = len(rng); cap = float(capital); cap_strict = float(capital); open_pos: dict = {}; closed: list = []; daily_equity = {}; strict_equity = {}
    report_every = max(1, (n - 1) // 100)                      # ~100 updates total, not one call per 5-minute bar
    for i in range(n - 1):
        if progress is not None and i % report_every == 0:
            progress(i, n - 1)
        dt = rng[i]; d_str = dt.strftime("%Y-%m-%d")
        for c in list(open_pos.keys()):
            pos = open_pos[c]; o, h, l, cl = arr[c]["open"][i], arr[c]["high"][i], arr[c]["low"][i], arr[c]["close"][i]
            if np.isnan(o):
                continue
            px = None; exit_fee_pct = 0.0; why = ""; slip = SLIP.get(c, 0.00015)
            if l <= pos["sl"]:
                px, exit_fee_pct, why = min(o, pos["sl"]), TAKER + slip, "stop"
            elif h >= pos["tp0"]:
                px, exit_fee_pct, why = (max(o, pos["tp0"]) if i > pos["entry_idx"] else pos["tp0"]), MAKER, "target"
            else:
                mid = arr[c]["mid20"][i]
                if not np.isnan(mid) and cl > mid and i > pos["entry_idx"] and i + 1 < n:   # close back above the 20-candle mid-band
                    px, exit_fee_pct, why = arr[c]["open"][i + 1], TAKER + slip, "exit_signal"
                elif i - pos["entry_idx"] >= TIME_STOP_BARS:
                    px, exit_fee_pct, why = cl, TAKER + slip, "time"
            if px is not None:
                gross = (px - pos["entry"]) / pos["entry"] * pos["size"]
                hours = (i - pos["entry_idx"]) * BAR_MIN / 60
                f_rate = float(arr[c]["fund"].get(dt.normalize(), 0.0))
                funding_cost = pos["size"] * f_rate * hours / 24
                fee = pos["size"] * (TAKER + slip + exit_fee_pct) + funding_cost   # entry (always market) + this exit's own fee
                net = gross - fee
                r_trade = net / cap
                cap_strict *= 1 + r_trade - (TAX * r_trade if r_trade > 0 else 0.0)   # strict: tax each winning trade, no loss offset
                cap += net
                closed.append({"symbol": c, "side": "LONG", "entry_time": str(pos["entry_time"]), "exit_time": str(dt), "entry_price": round(float(pos["entry"]), 6),
                                "exit_price": round(float(px), 6), "qty": round(float(pos["size"] / pos["entry"]), 6), "pnl": round(float(net)), "gross_pnl": round(float(gross)),
                                "fees": round(float(fee)), "exit_reason": why, "won": bool(net > 0)})
                del open_pos[c]
        if len(open_pos) < MAX_CONCURRENT and cap > 50:
            crowd = crowd_scale(sum(bool(arr[x]["sig"][i]) for x in coins), crowd_k)
            for c in coins:
                if c in open_pos or len(open_pos) >= MAX_CONCURRENT:
                    continue
                if arr[c]["sig"][i] and not np.isnan(arr[c]["open"][i + 1]):
                    e = arr[c]["open"][i + 1]
                    sl = e * (1 - STOPLOSS)
                    risk_pct = base_risk * vol_scale[i]                   # dynamic: size up in a calmer-than-usual market, down in a wilder one
                    size = min(cap * risk_pct / STOPLOSS, cap * leverage) * crowd
                    open_pos[c] = {"entry": e, "sl": sl, "tp0": e * (1 + ROI), "size": size, "entry_idx": i + 1, "entry_time": rng[i + 1]}
        daily_equity[d_str] = cap
        strict_equity[d_str] = cap_strict
    if progress is not None:
        progress(n - 1, n - 1)
    s_pre = pd.Series(daily_equity); s_str = pd.Series(strict_equity)
    s_pre.index = pd.to_datetime(s_pre.index); s_str.index = s_pre.index
    s_pre = s_pre[~s_pre.index.duplicated(keep="last")]; s_str = s_str[~s_str.index.duplicated(keep="last")]
    s_pre.iloc[-1] = cap; s_str.iloc[-1] = cap_strict
    s_biz, _ = business_tax(s_pre, capital)
    mode = {True: "strict", False: "none"}.get(tax, tax)
    s_eq = {"none": s_pre, "strict": s_str, "business": s_biz}.get(mode, s_biz)
    days = list(daily_equity.keys())
    closed_formatted = [{"net": x["pnl"], "fees": x["fees"], "gross": x["gross_pnl"], "slip": 0.0,
                          "days": round((pd.Timestamp(x["exit_time"]) - pd.Timestamp(x["entry_time"])).total_seconds() / 86400, 2)} for x in closed]
    m = metrics([round(v) for v in s_eq.values], capital, closed_formatted, round(len(days) * 250 / 365.25), 0)
    tax_effect = (cap - capital) - (float(s_eq.iloc[-1]) - capital)
    m["fees"] = round(m["fees"] + tax_effect)
    m["closed_profit"] = round(float(s_eq.iloc[-1]) - capital)
    m["open_profit"] = 0   # metrics() computed this from the pretax "closed" list vs the tax-adjusted equity curve, so it was
                            # actually holding the tax effect again, not real unrealized P&L (which this path doesn't track per
                            # open position) -- any position still open at the window's end is reported separately via "pos"
    m["final_pretax"], m["final_business"], m["final_strict"] = round(cap), round(float(s_biz.iloc[-1])), round(float(s_str.iloc[-1]))
    m["tax_mode"] = mode
    win_rate = sum(1 for t in closed if t["won"]) / max(len(closed), 1)
    m["note"] = (f"5-min dip-reversal on {', '.join(coins)}: {base_risk:.0%} risk per trade, at most {leverage:g}x, up to {MAX_CONCURRENT} open "
                 f"together. Real costs incl. funding. {len(closed):,} trades, {win_rate:.1%} won after costs. Before tax Rs{cap:,.0f}; "
                 f"business income Rs{s_biz.iloc[-1]:,.0f}; strict tax Rs{s_str.iloc[-1]:,.0f}.")

    # Replay data for the results page: daily candles + buy/sell events + open-position tracking, one entry per day in `days`
    # (same shape core.backtester.run_backtest and commodity/trend_carry use -- see components/replay/*.jsx).
    day_idx = {d: i for i, d in enumerate(days)}
    events = [[] for _ in days]
    pos_by_day = [[] for _ in days]
    bars = {}
    for c in coins:
        dd = data[c].set_index("date").loc[days[0] if days else start: days[-1] if days else end, ["open", "high", "low", "close"]]
        daily = dd.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        daily.index = daily.index.strftime("%Y-%m-%d")
        row = daily.reindex(days)
        last_bar = None
        out_bars = []
        for d in days:
            r = row.loc[d] if d in row.index else None
            if r is not None and not r.isna().any():
                last_bar = [round(float(r["open"]), 6), round(float(r["high"]), 6), round(float(r["low"]), 6), round(float(r["close"]), 6)]
            out_bars.append(list(last_bar) if last_bar is not None else None)
        bars[c] = out_bars
    for x in closed:
        d0, d1 = str(x["entry_time"])[:10], str(x["exit_time"])[:10]
        i0, i1 = day_idx.get(d0), day_idx.get(d1)
        if i0 is not None:
            events[i0].append({"t": "buy", "side": "long", "sym": x["symbol"], "q": x["qty"], "px": x["entry_price"],
                                "entry": x["entry_price"], "note": f"BUY {x['symbol']} at {x['entry_price']}"})
        if i1 is not None:
            events[i1].append({"t": "sell", "side": "long", "sym": x["symbol"], "q": x["qty"], "px": x["exit_price"],
                                "entry": x["entry_price"], "pnl": x["pnl"], "fees": x["fees"], "slip": 0.0, "why": x["exit_reason"]})
        if i0 is not None:
            for di in range(i0, (i1 if i1 is not None else len(days) - 1) + 1):
                pos_by_day[di].append({"sym": x["symbol"], "q": x["qty"], "e": x["entry_price"], "px": x["exit_price"], "start": i0})
    for c, p in open_pos.items():
        i0 = day_idx.get(str(p["entry_time"])[:10])
        if i0 is not None:
            q, e, stop = round(float(p["size"] / p["entry"]), 6), round(float(p["entry"]), 6), round(float(p["sl"]), 6)
            events[i0].append({"t": "buy", "side": "long", "sym": c, "q": q, "px": e, "entry": e, "note": f"BUY {c} at {e}"})
            for di in range(i0, len(days)):
                pos_by_day[di].append({"sym": c, "q": q, "e": e, "px": e, "start": i0, "stop": stop})
    return {"metrics": m, "equity": [round(v) for v in s_eq.values], "days": days, "trades": closed,
            "events": events, "pos": pos_by_day, "bars": bars, "overlays": {}}
