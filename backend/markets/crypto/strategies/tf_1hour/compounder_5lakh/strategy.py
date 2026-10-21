"""crypto / compounder_5lakh -- 1-hour trend/momentum trader on BTC, ETH, SOL perpetual futures (one position at a time).

Rules (1-hour finished candles, Binance USDT-M futures prices):
  * regime gate on BTC: close > EMA200 and ADX(14) > 18 -> longs only; close < EMA200 and ADX > 18 -> shorts only; else cash
  * pick the coin with the strongest (longs) / weakest (shorts) 24-hour return; enter when that return is beyond +-3% or RSI(14) < 38 /
    > 62; BTC and ETH also need their own ADX > 22
  * stop 1.5 x ATR(14), target 3.8 x ATR(14), time exit after 48 hours; entry at the signal candle's close (a market order)
  * size: RISK_PCT of the account at the stop (2%; 70% of that after 1 loss, 45% after 2+), at most LEVERAGE x the account
Costs (fixed 2026-10-21, user: "fix all issues"): entry and stop/time exits pay the CoinDCX taker fee 0.05% + 18% GST + slippage; the
take-profit (a resting limit order) pays the maker fee 0.02% + GST; a stop the candle OPENS beyond fills at that open; real funding
(Binance daily sums, pro-rated by the hours held; longs pay, shorts receive). Tax views: none / strict (31.2% of each winning trade) /
business income (31.2% of each April-March year's net, losses carried 4 years).
Results after these fixes (Rs5L, BTC+ETH+SOL): see markets/README.md. Before them the page showed Rs5L -> Rs63L; that came from maker
fees on every side with no GST, slippage, funding or tax, at 4% risk. Leverage is NOT risk-free: a 3.8x position loses 3.8x the move.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.backtester import metrics

DATA_DIR = ARCHIVE_ROOT / "crypto"
COINS = ("BTC", "ETH", "SOL")
RISK_PCT = 0.02                                   # was 0.04: with real costs 4% had a -64% worst fall, 2% about -35%
TAKER = 0.0005 * 1.18                             # CoinDCX futures market order incl. 18% GST
MAKER = 0.0002 * 1.18                             # limit order (the take-profit) incl. GST
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001}
TAX = 0.312


def _funding(coin: str) -> pd.Series:
    f = DATA_DIR / f"{coin}_funding_daily.csv"
    return pd.read_csv(f, index_col="date", parse_dates=True)["fundingRate"] if f.exists() else pd.Series(dtype=float)


def business_tax(curve: pd.Series, capital: float, rate: float = TAX, carry_years: int = 4) -> tuple[pd.Series, float]:
    """Tax on each April-March year's net profit (losses carried 4 years), paid at 31 March and on the last day. curve = before-tax value."""
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


def available() -> list[str]:
    return [c for c in COINS if (DATA_DIR / f"{c}USDT_1h.csv").exists()]


_DATA_CACHE: dict = {}


def load_1h_data(coins: list[str]) -> dict:
    global _DATA_CACHE
    missing = [c for c in coins if c not in _DATA_CACHE]
    for c in missing:
        f = DATA_DIR / f"{c}USDT_1h.csv"
        df = pd.read_csv(f)
        df["dt"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
        df.sort_values("dt", inplace=True)
        df.reset_index(drop=True, inplace=True)

        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / np.maximum(avg_loss, 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs()
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(14).mean()

        up_move = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1) - df["low"]
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        plus_di = 100 * (pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
        minus_di = 100 * (pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
        dx = 100 * (plus_di - minus_di).abs() / np.maximum(plus_di + minus_di, 1e-9)
        df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

        _DATA_CACHE[c] = df.set_index("dt")
    return {c: _DATA_CACHE[c] for c in coins}


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, leverage: float = 3.8, tax="business", **kwargs) -> dict:
    """Run full backtest for web dashboard."""
    coins = [s for s in (symbols or available()) if s in available()]
    if "BTC" not in coins:
        coins.append("BTC")
        
    d_map = load_1h_data(coins)
    common_idx = sorted(list(set.intersection(*(set(d_map[c].index) for c in coins))))
    t_start = pd.Timestamp(start)
    t_end = pd.Timestamp(end)
    date_range = [t for t in common_idx if t_start <= t <= t_end]
    
    if len(date_range) < 24:
        raise ValueError(f"Insufficient crypto data between {start} and {end}")

    ret24 = pd.DataFrame({c: d_map[c]["close"].pct_change(24) for c in coins}).reindex(date_range).fillna(0.0)

    # Pre-extract numpy arrays for 100x faster execution
    n_bars = len(date_range)
    close_arr = {c: d_map[c].reindex(date_range)["close"].to_numpy() for c in coins}
    open_arr = {c: d_map[c].reindex(date_range)["open"].to_numpy() for c in coins}
    fund = {c: _funding(c) for c in coins}
    high_arr = {c: d_map[c].reindex(date_range)["high"].to_numpy() for c in coins}
    low_arr = {c: d_map[c].reindex(date_range)["low"].to_numpy() for c in coins}
    atr_arr = {c: d_map[c].reindex(date_range)["atr"].to_numpy() for c in coins}
    rsi_arr = {c: d_map[c].reindex(date_range)["rsi"].to_numpy() for c in coins}
    adx_arr = {c: d_map[c].reindex(date_range)["adx"].to_numpy() for c in coins}
    ema200_arr = {c: d_map[c].reindex(date_range)["ema200"].to_numpy() for c in coins}
    ret24_arr = {c: ret24[c].to_numpy() for c in coins}
    trade_candidates = [c for c in coins if c in ("SOL", "ETH", "BTC")]

    cur_cap = float(capital)
    risk_pct = RISK_PCT
    sl_mult = 1.5
    tp_mult = 3.8
    maker_fee = 0.0002

    in_pos = False
    cur_coin = None
    pos_side = 0
    entry_px = 0.0
    pos_size_usd = 0.0
    sl_px = 0.0
    tp_px = 0.0
    entry_idx = 0
    consec_losses = 0
    cap_strict = float(capital)                                   # the same trades under the strict tax (each winning trade taxed)
    strict_equity = {}

    all_tr = []
    daily_equity = {}

    for idx, dt in enumerate(date_range):
        d_str = dt.strftime("%Y-%m-%d")
        daily_equity[d_str] = cur_cap
        strict_equity[d_str] = cap_strict

        if in_pos:
            o = open_arr[cur_coin][idx]
            h = high_arr[cur_coin][idx]
            l = low_arr[cur_coin][idx]
            c = close_arr[cur_coin][idx]
            is_exit = False
            exit_px = c
            why = ""

            if pos_side == 1:
                if l <= sl_px: exit_px = min(o, sl_px); why = "Stop Loss (1.5x ATR)"; is_exit = True
                elif h >= tp_px: exit_px = tp_px; why = "Take Profit (3.8x ATR)"; is_exit = True
                elif (idx - entry_idx) >= 48: exit_px = c; why = "Time Stop (48h)"; is_exit = True
            else:
                if h >= sl_px: exit_px = max(o, sl_px); why = "Stop Loss (1.5x ATR)"; is_exit = True
                elif l <= tp_px: exit_px = tp_px; why = "Take Profit (3.8x ATR)"; is_exit = True
                elif (idx - entry_idx) >= 48: exit_px = c; why = "Time Stop (48h)"; is_exit = True

            if is_exit:
                pnl = (pos_side * (exit_px - entry_px) / entry_px) * pos_size_usd
                slip = SLIP.get(cur_coin, 0.00015)
                exit_cost = MAKER if why.startswith("Take Profit") else TAKER + slip
                hours = idx - entry_idx
                f_rate = float(fund[cur_coin].get(dt.normalize(), 0.0)) if len(fund[cur_coin]) else 0.0
                funding = pos_side * pos_size_usd * f_rate * hours / 24      # daily sum pro-rated by the hours held
                fee = pos_size_usd * (TAKER + slip + exit_cost) + funding     # fees + slippage + funding
                net = pnl - fee
                r_trade = net / cur_cap
                cap_strict *= 1 + r_trade - (TAX * r_trade if r_trade > 0 else 0.0)
                cur_cap += net
                if net > 0:
                    consec_losses = 0
                else:
                    consec_losses += 1

                all_tr.append({
                    "coin": cur_coin, "side": "long" if pos_side == 1 else "short",
                    "t0": int(date_range[entry_idx].timestamp() // 60),
                    "t1": int(dt.timestamp() // 60),
                    "entry": float(entry_px), "exit": float(exit_px),
                    "pnl_rs": float(net), "gross": float(pnl), "net": float(net),
                    "fees": float(fee), "q": float(pos_size_usd / entry_px),
                    "why": why, "won": net > 0
                })
                in_pos = False
                cur_coin = None

        if not in_pos and cur_cap > 50.0:
            btc_c = close_arr["BTC"][idx]
            btc_ema200 = ema200_arr["BTC"][idx]
            btc_adx = adx_arr["BTC"][idx]
            is_bull = (btc_c > btc_ema200) and (btc_adx > 18.0)
            is_bear = (btc_c < btc_ema200) and (btc_adx > 18.0)

            best_c = trade_candidates[0]
            best_val = ret24_arr[best_c][idx]
            worst_c = trade_candidates[0]
            worst_val = ret24_arr[worst_c][idx]
            for c in trade_candidates[1:]:
                v = ret24_arr[c][idx]
                if v > best_val: best_val = v; best_c = c
                if v < worst_val: worst_val = v; worst_c = c

            sig = 0
            chosen = None
            if is_bull:
                if best_val > 0.03: sig = 1; chosen = best_c
                elif rsi_arr[best_c][idx] < 38.0: sig = 1; chosen = best_c
            elif is_bear:
                if worst_val < -0.03: sig = -1; chosen = worst_c
                elif rsi_arr[worst_c][idx] > 62.0: sig = -1; chosen = worst_c

            if sig != 0 and chosen is not None:
                # Dynamic Chop Filter: BTC and ETH require ADX > 22 to filter false breakouts during sideways chop
                if chosen in ("BTC", "ETH") and adx_arr[chosen][idx] < 22.0:
                    continue

                atr = atr_arr[chosen][idx]
                entry = close_arr[chosen][idx]
                if atr > 0:
                    sl_dist = atr * sl_mult
                    sl_pct = sl_dist / entry

                    # Dynamic Streak Risk Dampener: cushions drawdown during chop regimes
                    dyn_risk = risk_pct
                    if consec_losses == 1:
                        dyn_risk *= 0.70
                    elif consec_losses >= 2:
                        dyn_risk *= 0.45

                    pos_size_usd = min((cur_cap * dyn_risk) / sl_pct, cur_cap * leverage)

                    in_pos = True
                    cur_coin = chosen
                    pos_side = sig
                    entry_px = entry
                    entry_idx = idx
                    sl_px = entry - sl_dist if sig == 1 else entry + sl_dist
                    tp_px = entry + (sl_dist * tp_mult) if sig == 1 else entry - (sl_dist * tp_mult)

    days = pd.date_range(t_start.floor("D"), t_end.floor("D"), freq="D")
    s_pre = pd.Series(daily_equity).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_str = pd.Series(strict_equity).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre.index = pd.to_datetime(s_pre.index); s_str.index = s_pre.index
    s_pre.iloc[-1] = cur_cap; s_str.iloc[-1] = cap_strict
    s_biz, biz_tax = business_tax(s_pre, capital)
    mode = {True: "strict", False: "none"}.get(tax, tax)
    s_eq = {"none": s_pre, "strict": s_str, "business": s_biz}[mode]

    # Daily bars for dashboard chart
    bars = {}
    for c in coins:
        df_c = d_map[c].reindex(date_range)
        dd = df_c.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).reindex(days).ffill().bfill()
        bars[c] = [[round(float(v), 4) for v in r] for r in dd[["open", "high", "low", "close"]].values]

    closed_formatted = [
        {"net": x["net"], "fees": x["fees"], "gross": x["gross"], "slip": 0.0, "days": round((x["t1"] - x["t0"]) / 1440, 1)}
        for x in all_tr
    ]

    m = metrics([round(v) for v in s_eq.values], capital, closed_formatted, round(len(days) * 250 / 365.25), 0)
    tax_effect = (cur_cap - capital) - (m["final_equity"] - capital)            # tax (and the growth it would have earned)
    m["fees"] = round(m["fees"] + tax_effect); m["closed_profit"] = round(m["final_equity"] - capital); m["open_profit"] = 0

    events = [[] for _ in days]
    pos = [[] for _ in days]
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}

    for x in all_tr:
        dt_entry = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%Y-%m-%d")
        dt_exit = pd.Timestamp(x["t1"] * 60, unit="s").strftime("%Y-%m-%d")
        when = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%H:%M")
        hours = max(1.0, (x["t1"] - x["t0"]) / 60)

        # Standard buy/entry event
        if dt_entry in pos_of:
            events[pos_of[dt_entry]].append({
                "t": "buy",
                "side": x["side"],
                "sym": x["coin"],
                "q": round(x["q"], 4),
                "px": round(x["entry"], 4),
                "entry": round(x["entry"], 4),
                "stop": round(x.get("stop", x["entry"] * 0.95), 4),
                "note": f"{when} UTC {'BUY' if x['side'] == 'long' else 'SHORT'} ({x['coin']}) at ${x['entry']:,.2f}"
            })

        # Standard sell/exit event
        if dt_exit in pos_of:
            events[pos_of[dt_exit]].append({
                "t": "sell",
                "side": x["side"],
                "sym": x["coin"],
                "q": round(x["q"], 4),
                "px": round(x["exit"], 4),
                "entry": round(x["entry"], 4),
                "pnl": round(x["pnl_rs"], 2),
                "fees": round(x["fees"], 2),
                "slip": 0.0,
                "days": round(hours / 24, 1),
                "why": x["why"]
            })

        # Position tracking across calendar days
        i_start = pos_of.get(dt_entry)
        i_end = pos_of.get(dt_exit)
        if i_start is not None and i_end is not None:
            for d_idx in range(i_start, min(i_end, len(days))):
                pos[d_idx].append({
                    "sym": x["coin"],
                    "q": round(x["q"], 4),
                    "e": round(x["entry"], 4),
                    "px": round(x["exit"], 4),
                    "start": i_start
                })

    # Hourly chart data for full-fidelity interactive replay
    t0g = int(days[0].timestamp()) // 3600 * 3600
    t1g = int((days[-1] + pd.Timedelta(days=1)).timestamp())
    times = list(range(t0g, t1g, 3600))
    pos1h = {v: i for i, v in enumerate(times)}
    chart = {
        "tf": "1H",
        "times": times,
        "bars": {},
        "trades": {},
        "day_end": [min(len(times) - 1, max(0, (int((d + pd.Timedelta(days=1)).timestamp()) - t0g) // 3600 - 1)) for d in days]
    }
    for c in coins:
        df_c = d_map[c].reindex(date_range).dropna(subset=["close"])
        b1 = [None] * len(times)
        if len(df_c):
            ts_secs = (df_c.index.astype("int64") // 10**9 // 3600 * 3600).to_numpy()
            ov = df_c["open"].to_numpy()
            hv = df_c["high"].to_numpy()
            lv = df_c["low"].to_numpy()
            cv = df_c["close"].to_numpy()
            for ts_sec, o_, h_, l_, c_ in zip(ts_secs, ov, hv, lv, cv):
                gi = pos1h.get(ts_sec)
                if gi is not None and 0 <= gi < len(times):
                    b1[gi] = [round(float(o_), 4), round(float(h_), 4), round(float(l_), 4), round(float(c_), 4)]
        last_b = None
        for k_idx in range(len(b1)):
            if b1[k_idx] is not None:
                last_b = b1[k_idx]
            elif last_b is not None:
                b1[k_idx] = list(last_b)
        chart["bars"][c] = b1
        chart["trades"][c] = [
            {
                "i0": pos1h.get(x["t0"] * 60 // 3600 * 3600),
                "i1": pos1h.get(x["t1"] * 60 // 3600 * 3600),
                "entry": float(round(x["entry"], 4)),
                "exit": float(round(x["exit"], 4)),
                "qty": round(x["q"], 4),
                "soldQty": round(x["q"], 4),
                "pnl": round(x["pnl_rs"], 2),
                "side": x["side"],
                "adds": []
            }
            for x in all_tr if x["coin"] == c and pos1h.get(x["t0"] * 60 // 3600 * 3600) is not None
        ]

    first_px = np.array([b[0][3] for b in bars.values()])
    bench = (capital / len(coins) * np.array([[b[i][3] for b in bars.values()] for i in range(len(days))]) / first_px).sum(axis=1)

    return {
        "days": [d.strftime("%Y-%m-%d") for d in days],
        "equity": [round(v) for v in s_eq.values],
        "events": events,
        "pos": pos,
        "bars": bars,
        "chart": chart,
        "overlays": {},
        "skipped_symbols": [],
        "benchmark": [round(v) for v in bench],
        "metrics": m | {
            "tax_mode": mode, "final_pretax": round(cur_cap), "final_strict": round(cap_strict), "final_business": round(float(s_biz.iloc[-1])),
            "note": f"1-hour compounder on {', '.join(coins)}: {RISK_PCT:.0%} risk per trade, at most {leverage:g}x. Real costs: taker fee + GST + slippage on "
                    f"entries and stops, maker fee + GST on take-profits, gapped stops at the open, real funding. {len(all_tr):,} trades, "
                    f"{sum(1 for x in all_tr if x['won']) / max(len(all_tr), 1):.1%} won after costs. Before tax Rs{cur_cap:,.0f}; business income "
                    f"Rs{s_biz.iloc[-1]:,.0f}; strict tax Rs{cap_strict:,.0f}."
        }
    }
