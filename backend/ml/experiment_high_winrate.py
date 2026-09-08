#!/usr/bin/env python3
"""
ml/experiment_high_winrate.py — Systematic Trial-and-Error ML Architecture Benchmarking.

Trains and compares:
  1. LightGBM (Focal / Binary logloss)
  2. CatBoost (Symmetric Oblivious Trees)
  3. XGBoost (Gradient Boosted Trees)
  4. Stacked Voting Ensemble (LightGBM + CatBoost + XGBoost)

Features Triple-Barrier Labeling (+2.0R Target vs -1.0R Stop), 18 Microstructure Features,
and Out-of-Sample Walk-Forward Backtesting with exact Indian MCX statutory costs.
"""
from __future__ import annotations

import sys
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import pickle

sys.path.insert(0, str(Path(__file__).parent.parent))

import lightgbm as lgb
import xgboost as xgb
from catboost import CatBoostClassifier
from sklearn.metrics import roc_auc_score, precision_score, accuracy_score
import pandas_ta_classic as ta

from strategy.commodity_costs import compute_mcx_commodity_costs, size_commodity_lots

ARCHIVE_DIR = Path(__file__).parent.parent / "archive_commodities"


def extract_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c, h, l, o, v = out["close"], out["high"], out["low"], out["open"], out["volume"]

    # 1. Microstructure Price Location & Direction
    rng = (h - l).replace(0, np.nan)
    out["close_loc_in_bar"] = ((c - l) / rng).fillna(0.5)
    out["bar_direction"] = np.where(c > o, 1.0, np.where(c < o, -1.0, 0.0))

    # 2. Volume Pressure / Order Flow Proxy (buying pressure vs selling pressure in wicks)
    upper_wick = (h - np.maximum(c, o)).fillna(0.0)
    lower_wick = (np.minimum(c, o) - l).fillna(0.0)
    body = np.abs(c - o).fillna(0.0)
    buying_pressure = (lower_wick + (c - o).clip(lower=0)) / rng
    out["buying_pressure_ratio"] = buying_pressure.fillna(0.5)

    # 3. Volume Surge
    vol_roll = v.rolling(20, min_periods=1).mean()
    out["vol_surge_ratio"] = (v / vol_roll.replace(0, np.nan)).fillna(1.0)

    # 4. Volatilities & Compression
    ratio = np.log(h.replace(0, np.nan) / l.replace(0, np.nan)).fillna(0.0)
    out["parkinson_vol_5"] = np.sqrt((ratio ** 2).rolling(5, min_periods=1).mean() / (4.0 * np.log(2.0))) * 100.0
    out["parkinson_vol_20"] = np.sqrt((ratio ** 2).rolling(20, min_periods=1).mean() / (4.0 * np.log(2.0))) * 100.0
    out["vol_compression_ratio"] = (out["parkinson_vol_5"] / out["parkinson_vol_20"].replace(0, np.nan)).fillna(1.0)

    # 5. Technical Indicators
    out["ema_9"] = ta.ema(c, length=9)
    out["ema_21"] = ta.ema(c, length=21)
    out["ema_50"] = ta.ema(c, length=50)
    out["ema_slope_pct"] = out["ema_9"].pct_change(3) * 100.0
    out["trend_alignment"] = np.where((out["ema_9"] > out["ema_21"]) & (out["ema_21"] > out["ema_50"]), 1.0,
                             np.where((out["ema_9"] < out["ema_21"]) & (out["ema_21"] < out["ema_50"]), -1.0, 0.0))

    # RSI & Momentum
    out["rsi_14"] = ta.rsi(c, length=14).fillna(50.0)
    out["mom_pct"] = c.pct_change(5) * 100.0

    # ADX & DMI
    try:
        adx_df = ta.adx(h, l, c, length=14)
        if adx_df is not None:
            out["adx"] = adx_df["ADX_14"].fillna(25.0)
            out["dmp"] = adx_df["DMP_14"].fillna(25.0)
            out["dmn"] = adx_df["DMN_14"].fillna(25.0)
            out["dmi_spread"] = out["dmp"] - out["dmn"]
        else:
            out["adx"], out["dmi_spread"] = 25.0, 0.0
    except Exception:
        out["adx"], out["dmi_spread"] = 25.0, 0.0

    # Bollinger Bandwidth
    try:
        bb = ta.bbands(c, length=20, std=2.0)
        if bb is not None:
            out["bb_bandwidth"] = (bb["BBU_20_2.0"] - bb["BBL_20_2.0"]) / bb["BBM_20_2.0"] * 100.0
        else:
            out["bb_bandwidth"] = 1.0
    except Exception:
        out["bb_bandwidth"] = 1.0

    # ATR
    try:
        atr_s = ta.atr(h, l, c, length=14)
        out["atr"] = atr_s.bfill().ffill() if atr_s is not None else (h - l).rolling(14).mean()
    except Exception:
        out["atr"] = (h - l).rolling(14).mean()

    # Intraday VWAP
    dt_s = pd.to_datetime(out["timestamp"])
    out["_date"] = dt_s.dt.date
    out["minutes_since_open"] = (dt_s.dt.hour * 60 + dt_s.dt.minute) - (9 * 60)
    out["cum_vol"] = out.groupby("_date")["volume"].cumsum()
    out["cum_pv"] = out.groupby("_date").apply(lambda g: (g["close"] * g["volume"]).cumsum()).reset_index(level=0, drop=True)
    out["vwap"] = (out["cum_pv"] / out["cum_vol"]).fillna(c)
    out["vwap_dist_pct"] = (c - out["vwap"]) / out["vwap"] * 100.0

    # ORB Breakout (first 6 bars)
    def _orb(g):
        n_orb = min(6, len(g))
        oh = g["high"].iloc[:n_orb].max() if n_orb > 0 else g["high"].iloc[0]
        ol = g["low"].iloc[:n_orb].min() if n_orb > 0 else g["low"].iloc[0]
        g["orb_high_dist_pct"] = (g["close"] - oh) / oh * 100.0
        g["orb_low_dist_pct"] = (g["close"] - ol) / ol * 100.0
        return g

    out = out.groupby("_date", group_keys=False).apply(_orb)
    out.drop(columns=["_date", "cum_vol", "cum_pv"], inplace=True, errors="ignore")
    return out


FEATURE_COLS = [
    "close_loc_in_bar", "bar_direction", "buying_pressure_ratio", "vol_surge_ratio",
    "parkinson_vol_5", "vol_compression_ratio", "ema_slope_pct", "trend_alignment",
    "rsi_14", "mom_pct", "adx", "dmi_spread", "bb_bandwidth", "vwap_dist_pct",
    "orb_high_dist_pct", "orb_low_dist_pct"
]


def create_triple_barrier_labels(df: pd.DataFrame, target_mult: float = 2.0, stop_mult: float = 1.4, max_bars: int = 16) -> pd.Series:
    """
    Triple-barrier method:
      y = 1 if price reaches +2.0R profit target BEFORE hitting -1.0R stop loss within max_bars.
      y = 0 otherwise.
    """
    n = len(df)
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    atr = df["atr"].values

    labels = np.zeros(n, dtype=int)

    for i in range(n - max_bars):
        entry = c[i]
        sdist = max(stop_mult * atr[i], 0.0035 * entry)
        tp_level = entry + target_mult * sdist
        sl_level = entry - sdist

        hit = 0
        for j in range(i + 1, min(i + max_bars + 1, n)):
            if h[j] >= tp_level:
                hit = 1
                break
            if l[j] <= sl_level:
                hit = 0
                break
        labels[i] = hit

    return pd.Series(labels, index=df.index)


def evaluate_backtest(df_test: pd.DataFrame, p_ups: np.ndarray, threshold: float = 0.54, sym: str = "CRUDEOILM") -> dict:
    closes = df_test["close"].values
    highs = df_test["high"].values
    lows = df_test["low"].values
    mins_open = df_test["minutes_since_open"].values
    timestamps = df_test["timestamp"].values
    adxs = df_test["adx"].values
    dmi_spreads = df_test["dmi_spread"].values
    vol_surges = df_test["vol_surge_ratio"].values
    vwap_ds = df_test["vwap_dist_pct"].values
    ema_slopes = df_test["ema_slope_pct"].values
    orb_h_dists = df_test["orb_high_dist_pct"].values
    orb_l_dists = df_test["orb_low_dist_pct"].values
    atrs = df_test["atr"].values

    n = len(df_test)
    capital = 100000.0
    trades = []
    in_pos = False
    pos = {}
    last_trade_day = None

    for i in range(n):
        c_price = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        c_time = str(timestamps[i])
        c_day = c_time[:10]
        m_open = mins_open[i]

        if in_pos:
            d = 1 if pos["direction"] == "long" else -1
            fav = c_high if d == 1 else c_low
            adv = c_low if d == 1 else c_high

            exit_p = None; reason = None
            if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
                exit_p = pos["tp"]; reason = "take_profit"
            elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
                exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
            elif (i - pos["entry_idx"]) >= 16:
                exit_p = c_price; reason = "timeout_exit"
            elif m_open >= 840:
                exit_p = c_price; reason = "eod_squareoff"

            if exit_p is not None:
                cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos["lots"])
                capital += cost_info["net"]
                trades.append({**cost_info, "reason": reason})
                in_pos = False
                pos = {}
                continue
            else:
                if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                    pos["armed_be"] = True
                    pos["current_stop"] = pos["entry_price"] + 0.0008 * pos["entry_price"] * d
                if pos["armed_be"]:
                    pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                    trail = pos["best_price"] - 0.40 * pos["stop_dist"] * d
                    pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))
            continue

        if c_day == last_trade_day:
            continue
        if m_open < 570 or m_open > 780:
            continue

        p_up = p_ups[i]
        adx = adxs[i]
        dmi_s = dmi_spreads[i]
        vol_s = vol_surges[i]
        vwap_d = vwap_ds[i]
        ema_s = ema_slopes[i]
        orb_h = orb_h_dists[i]
        orb_l = orb_l_dists[i]
        atr = atrs[i]

        sdist = max(1.4 * atr, 0.0035 * c_price)
        if sdist <= 0 or c_price <= 0:
            continue

        direction = None
        if p_up >= threshold and adx >= 20 and dmi_s > 0 and ema_s > 0.010 and orb_h >= 0.05 and vwap_d >= 0.05 and vol_s >= 1.10:
            direction = "long"
        elif p_up <= (1.0 - threshold) and adx >= 20 and dmi_s < 0 and ema_s < -0.010 and orb_l <= -0.05 and vwap_d <= -0.05 and vol_s >= 1.10:
            direction = "short"

        if not direction:
            continue

        lots = size_commodity_lots(capital, c_price, sdist, 5.0, sym, 4.0)
        if lots == 0:
            continue

        in_pos = True
        last_trade_day = c_day
        d = 1 if direction == "long" else -1
        pos = {
            "direction": direction, "entry_price": c_price, "entry_idx": i, "lots": lots,
            "stop_dist": sdist, "current_stop": round(c_price - sdist * d, 2),
            "tp": round(c_price + 2.0 * sdist * d, 2),
            "be": round(c_price + 0.50 * sdist * d, 2),
            "best_price": c_price, "armed_be": False,
        }

    wins = [t for t in trades if t["net"] > 0]
    wr = len(wins) / len(trades) * 100 if trades else 0.0
    net_pnl = sum(t["net"] for t in trades)
    fees = sum(t["total"] for t in trades)
    gross_pnl = sum(t["gross"] for t in trades)
    profit_factor = (sum(t["net"] for t in wins) / abs(sum(t["net"] for t in trades if t["net"] <= 0))) if [t for t in trades if t["net"] <= 0] else 99.0

    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": round(wr, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "final_capital": round(capital, 2),
    }


def main():
    print("=" * 80)
    print("🔬 EMPIRICAL ML ARCHITECTURE TRIAL & ERROR EXPERIMENT (MCX CRUDE OIL)")
    print("=" * 80)

    csv_path = ARCHIVE_DIR / "CRUDEOIL_5minute.csv"
    raw_df = pd.read_csv(csv_path)
    print(f"Loaded {len(raw_df):,} 5-minute bars. Engineering 16 advanced microstructure features...")

    feat_df = extract_advanced_features(raw_df)
    labels = create_triple_barrier_labels(feat_df, target_mult=2.0, stop_mult=1.4, max_bars=16)
    feat_df["label"] = labels

    # Train/Test Split (Train: 2022-2025, Test: 2026 Walk-Forward)
    dt = pd.to_datetime(feat_df["timestamp"])
    train_mask = dt.dt.year < 2026
    test_mask = (dt.dt.year == 2026) & (dt.dt.date <= pd.to_datetime("2026-09-07").date())

    train_df = feat_df[train_mask].dropna(subset=FEATURE_COLS)
    test_df = feat_df[test_mask].dropna(subset=FEATURE_COLS)

    X_train, y_train = train_df[FEATURE_COLS], train_df["label"]
    X_test, y_test = test_df[FEATURE_COLS], test_df["label"]

    print(f"Train Samples: {len(X_train):,} (Positive Rate: {y_train.mean()*100:.1f}%)")
    print(f"Test Samples:  {len(X_test):,}  (Positive Rate: {y_test.mean()*100:.1f}%)\n")

    # -------------------------------------------------------------
    # 1. Model A: LightGBM
    # -------------------------------------------------------------
    print("--- 1. Training LightGBM Model ---")
    lgb_model = lgb.LGBMClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.03,
        num_leaves=31, subsample=0.8, colsample_bytree=0.8,
        random_state=42, verbose=-1
    )
    lgb_model.fit(X_train, y_train)
    p_lgb_test = lgb_model.predict_proba(X_test)[:, 1]
    auc_lgb = roc_auc_score(y_test, p_lgb_test)
    print(f"LightGBM Out-of-Sample AUC: {auc_lgb:.4f}")

    # -------------------------------------------------------------
    # 2. Model B: CatBoost
    # -------------------------------------------------------------
    print("\n--- 2. Training CatBoost Model ---")
    cb_model = CatBoostClassifier(
        iterations=300, depth=6, learning_rate=0.03,
        random_seed=42, verbose=False
    )
    cb_model.fit(X_train, y_train)
    p_cb_test = cb_model.predict_proba(X_test)[:, 1]
    auc_cb = roc_auc_score(y_test, p_cb_test)
    print(f"CatBoost Out-of-Sample AUC: {auc_cb:.4f}")

    # -------------------------------------------------------------
    # 3. Model C: XGBoost
    # -------------------------------------------------------------
    print("\n--- 3. Training XGBoost Model ---")
    xgb_model = xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, eval_metric="logloss"
    )
    xgb_model.fit(X_train, y_train)
    p_xgb_test = xgb_model.predict_proba(X_test)[:, 1]
    auc_xgb = roc_auc_score(y_test, p_xgb_test)
    print(f"XGBoost Out-of-Sample AUC: {auc_xgb:.4f}")

    # -------------------------------------------------------------
    # 4. Model D: Stacked Ensemble (Soft Voting)
    # -------------------------------------------------------------
    p_ensemble = (0.40 * p_lgb_test + 0.35 * p_cb_test + 0.25 * p_xgb_test)
    auc_ens = roc_auc_score(y_test, p_ensemble)
    print(f"\n--- 4. Stacked Ensemble (LGB + CatBoost + XGB) ---")
    print(f"Ensemble Out-of-Sample AUC: {auc_ens:.4f}")

    # -------------------------------------------------------------
    # Walk-Forward Backtest Comparisons across Models & Thresholds
    # -------------------------------------------------------------
    print("\n" + "=" * 85)
    print("📊 2026 WALK-FORWARD BACKTEST RESULTS ACROSS ML MODELS & THRESHOLDS")
    print("=" * 85)
    print(f"{'Model Architecture':28s} {'Threshold':10s} {'Trades':8s} {'Win Rate':10s} {'Profit Factor':15s} {'Net Realized PnL':18s}")
    print("-" * 85)

    experiments = [
        ("No ML (Pure Indicators)", np.full(len(X_test), 0.50), 0.50),
        ("LightGBM (Standard)", p_lgb_test, 0.54),
        ("CatBoost (Standard)", p_cb_test, 0.54),
        ("XGBoost (Standard)", p_xgb_test, 0.54),
        ("Stacked Ensemble (Standard)", p_ensemble, 0.54),
        ("Stacked Ensemble (High Conf)", p_ensemble, 0.60),
        ("Stacked Ensemble (A+ Ultra)", p_ensemble, 0.66),
    ]

    for name, probs, th in experiments:
        res = evaluate_backtest(test_df, probs, threshold=th)
        wr_str = f"{res['win_rate']:.1f}%"
        net_str = f"₹{res['net_pnl']:+,.2f}"
        print(f"{name:28s} {th:10.2f} {res['trades']:<8d} {wr_str:10s} {res['profit_factor']:<15.2f} {net_str:18s}")

    print("=" * 85)


if __name__ == "__main__":
    main()
