"""crypto / dip_reversal -- 5-minute dip-buy reversal on BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK perpetual futures.

Found by screening ~60 public strategies from github.com/freqtrade/freqtrade-strategies (`CombinedBinHAndCluc`) on our own coin data
with real costs, then re-optimised: the only one of the ~60 that stayed profitable in both a 2022-24 dev window and a 2025-26 held-out
test window, and whose profit survives nudging every setting up or down (not a lucky parameter) -- see markets/README.md.

Rule (5-minute finished candles, no indicator peeking at the still-forming candle):
  * enter long when EITHER (a) a sharp, one-sided Bollinger-band squeeze: the 40-candle band is unusually wide, the candle just closed
    well below its lower band and below the previous close with hardly any lower wick (BinHV45's rule), OR (b) price is below its
    50-candle EMA and has slipped meaningfully under its 20-candle Bollinger low band (ClucMay2018's rule)
  * exit: hitting the 7% take-profit (a resting limit order), the 7% stop, the price closing back above the 20-candle Bollinger
    mid-band (a market exit), or a 24-hour time stop if none of those trigger (our own safety cap -- the original rule has no time stop)
  * entry at the next candle's open (a market order); size = RISK_PCT of the account / the 7% stop distance (so ~28.6% of the account
    at RISK_PCT=2%), capped at LEVERAGE x the account as a pure safety ceiling -- the real throttle on profit and risk here is
    RISK_PCT, not leverage (a fixed-% stop means leverage rarely binds; see the risk_pct table in markets/README.md before raising it);
    at most MAX_CONCURRENT positions open together across the 9 coins (shared margin pool, and crypto coins move together in a crash)
Costs: CoinDCX taker fee 0.05% + 18% GST + slippage on entries, stops and the time exit; maker fee 0.02% + GST on the take-profit; a
stop the candle OPENS beyond fills at that open, not the stop price; real funding (Binance daily sums, pro-rated by hours held -- a
long position pays when funding is positive). Tax views: none / strict (31.2% of each winning trade) / business income (31.2% of each
April-March year's net, losses carried 4 years; the default and the honest number to look at).
2026-10-21 production backtest (one shared capital pool, max 3 concurrent positions, RISK_PCT=2%, Rs1L): dev (2022-24) Rs1.82L business
income (worst fall -19%), test (2025-26) Rs1.17L business income (worst fall -11%). Raising RISK_PCT makes more but falls further (3%:
Rs2.40L/-27%, dev; 4%: Rs3.12L/-34%; 6%: Rs4.95L/-47% -- test only gains a little past 2-3% while the worst fall grows fast); 2% is the
shipped default as the best balance. Leverage is a safety CEILING on position size here, not a throttle -- the 7% stop already fixes
position size at RISK_PCT / 7%, so changing leverage rarely changes a trade's size or the P&L above.
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
COINS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK")

RISK_PCT = 0.02
MAX_CONCURRENT = 3                                # shared margin pool + crypto correlates hard in a crash; see equity's MAX_CONCURRENT lesson
TAKER = 0.0005 * 1.18                              # CoinDCX futures market order incl. 18% GST
MAKER = 0.0002 * 1.18                              # limit order (the take-profit) incl. GST
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001}
BBDELTA_MULT = 0.008
CLOSEDELTA_MULT = 0.0175
TAIL_MULT = 0.25
CLUC_MULT = 0.985
STOPLOSS = 0.07
ROI = 0.07
TIME_STOP_BARS = 288                               # 24h of 5-min candles -- our own safety cap, the original rule has none
BAR_MIN = 5


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


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, leverage: float = 3.0, tax: str = "business", **kwargs) -> dict:
    coins = [c.upper() for c in (symbols or list(COINS)) if c.upper() in COINS]
    if not coins:
        raise ValueError("No usable coins")
    data = load_5m_data(coins)
    rng = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(days=1), freq=f"{BAR_MIN}min", tz=None)
    rng = rng[(rng >= pd.Timestamp(start)) & (rng <= pd.Timestamp(end) + pd.Timedelta(days=1))]
    arr = {}
    for c in coins:
        d = data[c].set_index("date").reindex(rng)
        arr[c] = {k: d[k].to_numpy() for k in ("open", "high", "low", "close")}
        arr[c]["sig"] = entry_signal(d).fillna(False).to_numpy()
        arr[c]["mid20"] = d["bb_mid20"].to_numpy()
        arr[c]["fund"] = _funding(c)
    n = len(rng); cap = float(capital); cap_strict = float(capital); open_pos: dict = {}; closed: list = []; daily_equity = {}; strict_equity = {}
    for i in range(n - 1):
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
                closed.append({"symbol": c, "side": "LONG", "entry_time": str(pos["entry_time"]), "exit_time": str(dt), "entry_price": round(pos["entry"], 6),
                                "exit_price": round(px, 6), "qty": round(pos["size"] / pos["entry"], 6), "pnl": round(net), "gross_pnl": round(gross),
                                "fees": round(fee), "exit_reason": why, "won": net > 0})
                del open_pos[c]
        if len(open_pos) < MAX_CONCURRENT and cap > 50:
            for c in coins:
                if c in open_pos or len(open_pos) >= MAX_CONCURRENT:
                    continue
                if arr[c]["sig"][i] and not np.isnan(arr[c]["open"][i + 1]):
                    e = arr[c]["open"][i + 1]
                    sl = e * (1 - STOPLOSS)
                    size = min(cap * RISK_PCT / STOPLOSS, cap * leverage)
                    open_pos[c] = {"entry": e, "sl": sl, "tp0": e * (1 + ROI), "size": size, "entry_idx": i + 1, "entry_time": rng[i + 1]}
        daily_equity[d_str] = cap
        strict_equity[d_str] = cap_strict
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
    m["final_pretax"], m["final_business"], m["final_strict"] = round(cap), round(float(s_biz.iloc[-1])), round(float(s_str.iloc[-1]))
    m["tax_mode"] = mode
    win_rate = sum(1 for t in closed if t["won"]) / max(len(closed), 1)
    m["note"] = (f"5-min dip-reversal on {', '.join(coins)}: {RISK_PCT:.0%} risk per trade, at most {leverage:g}x, up to {MAX_CONCURRENT} open "
                 f"together. Real costs incl. funding. {len(closed):,} trades, {win_rate:.1%} won after costs. Before tax Rs{cap:,.0f}; "
                 f"business income Rs{s_biz.iloc[-1]:,.0f}; strict tax Rs{s_str.iloc[-1]:,.0f}.")
    return {"metrics": m, "equity": [round(v) for v in s_eq.values], "days": days, "trades": closed,
            "pos": [{"symbol": c, "side": "LONG", "qty": round(p["size"] / p["entry"], 6), "entry_price": round(p["entry"], 6)} for c, p in open_pos.items()]}
