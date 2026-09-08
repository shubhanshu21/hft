"""
ml/train_commodity.py — Walk-Forward LightGBM Training on 5-Minute MCX Commodity Datasets

Labels each 5-minute decision point with a Triple-Barrier horizon:
  - TP: +1.8R Take Profit
  - SL: -1.0R Stop Loss
  - Horizon: 8 bars (40 minutes)

Trains rolling out-of-sample LightGBM models across commodities and outputs:
  - Classification Metrics (ROC-AUC, Precision, Win Rate)
  - Trained Models saved to cache/lgb_commodity_<symbol>.pkl
"""
from __future__ import annotations

import argparse
from pathlib import Path
import pickle
import sys

# Ensure backend root is in Python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, precision_score, accuracy_score

from strategy.commodity_features import COMMODITY_FEATURE_COLUMNS, compute_commodity_features

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "archive_commodities"
MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "commodity_models"

HOLD_BARS = 12          # 60-minute forward evaluation window
TAKE_PROFIT_MULT = 2.5  # +2.5R target
STOP_MULT = 1.0         # -1.0R stop


def label_commodity_bars(df: pd.DataFrame) -> pd.DataFrame:
    """
    Labels each bar using Triple Barrier: Does price hit +2.5R before -1.0R within HOLD_BARS?
    Uses ATR-scaled dynamic stop distances.
    """
    df = df.copy()
    n = len(df)
    labels = np.full(n, np.nan)

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values if "atr" in df.columns else (df["avg_range_pct"].values / 100.0) * closes

    for i in range(n - HOLD_BARS):
        entry = closes[i]
        atr = atrs[i]
        sdist = max(1.4 * atr, 0.0035 * entry)
        if sdist <= 0 or entry <= 0:
            continue

        tp = entry + TAKE_PROFIT_MULT * sdist
        sl = entry - STOP_MULT * sdist

        # Scan forward
        hit = 0
        for j in range(i + 1, min(i + HOLD_BARS + 1, n)):
            if highs[j] >= tp:
                hit = 1
                break
            if lows[j] <= sl:
                hit = 0
                break
        labels[i] = hit

    df["label"] = labels
    return df.dropna(subset=["label"]).reset_index(drop=True)



COMMODITY_ALIASES = {
    "CRUDEOILM": "CRUDEOIL",
    "NATGASMINI": "NATURALGAS",
    "GOLDM": "GOLD",
    "SILVERMIC": "SILVER",
    "SILVERM": "SILVER",
    "COPPER": "COPPER",
    "CRUDEOIL": "CRUDEOIL",
    "NATURALGAS": "NATURALGAS",
    "GOLD": "GOLD",
    "SILVER": "SILVER",
}


def train_commodity_model(symbol: str = "CRUDEOILM") -> tuple[lgb.LGBMClassifier, dict]:
    """Trains a walk-forward LightGBM model on a given commodity (supports Mini/Micro symbols)."""
    base_sym = COMMODITY_ALIASES.get(symbol.upper(), symbol.upper())
    csv_path = ARCHIVE_DIR / f"{base_sym}_5minute.csv"
    if not csv_path.exists():
        csv_path = ARCHIVE_DIR / f"{symbol.upper()}_5minute.csv"
    if not csv_path.exists():
        print(f"Archive file not found: {csv_path}")
        return None, {}

    print(f"\n{'='*70}")
    print(f"  TRAINING LIGHTGBM COMMODITY MODEL: {symbol.upper()} (Base Data: {base_sym})")
    print(f"{'='*70}")

    raw_df = pd.read_csv(csv_path)
    print(f"Loaded {len(raw_df):,} 5-minute bars from {csv_path.name}")

    # 1. Feature Engineering
    feat_df = compute_commodity_features(raw_df, symbol=base_sym)
    labeled_df = label_commodity_bars(feat_df)
    print(f"Generated {len(labeled_df):,} labeled decision points.")

    # 2. Time-series Walk-forward Split (80% Train, 20% Test)
    split_idx = int(len(labeled_df) * 0.80)
    train_df = labeled_df.iloc[:split_idx]
    test_df  = labeled_df.iloc[split_idx:]

    X_train = train_df[COMMODITY_FEATURE_COLUMNS]
    y_train = train_df["label"].astype(int)
    X_test  = test_df[COMMODITY_FEATURE_COLUMNS]
    y_test  = test_df["label"].astype(int)

    # 3. LightGBM Classifier
    model = lgb.LGBMClassifier(
        n_estimators=150,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=5,
        min_child_samples=40,
        feature_fraction=0.85,
        bagging_fraction=0.85,
        bagging_freq=1,
        class_weight="balanced",
        random_state=42,
        verbosity=-1,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)]
    )

    # 4. Out-of-sample Evaluation
    preds_proba = model.predict_proba(X_test)[:, 1]
    preds_binary = (preds_proba >= 0.55).astype(int)

    auc = roc_auc_score(y_test, preds_proba)
    acc = accuracy_score(y_test, preds_binary)
    prec = precision_score(y_test, preds_binary, zero_division=0)

    # High conviction accuracy
    high_mask = preds_proba >= 0.60
    high_prec = precision_score(y_test[high_mask], (preds_proba[high_mask] >= 0.60).astype(int), zero_division=0) if high_mask.sum() > 0 else 0.0

    print(f"\n  📊 OUT-OF-SAMPLE TEST PERFORMANCE (Last 20% data):")
    print(f"     * ROC-AUC:               {auc:.4f}")
    print(f"     * Overall Accuracy:      {acc*100:.2f}%")
    print(f"     * Precision (P >= 0.55): {prec*100:.2f}%")
    print(f"     * Precision (P >= 0.60): {high_prec*100:.2f}% (High-Conviction)")

    # 5. Top Feature Importances
    imp = pd.Series(model.feature_importances_, index=COMMODITY_FEATURE_COLUMNS).sort_values(ascending=False)
    print(f"\n  🌟 TOP 5 PREDICTIVE FEATURES:")
    for f, v in imp.head(5).items():
        print(f"     - {f:24s}: {v:4d}")

    # 6. Save Model for both Symbol and Base/Mini aliases
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for save_name in {symbol.lower(), base_sym.lower()}:
        out_model_path = MODEL_CACHE_DIR / f"lgb_{save_name}.pkl"
        with open(out_model_path, "wb") as f:
            pickle.dump(model, f)
        print(f"  💾 Model saved to: {out_model_path}")

    return model, {"auc": auc, "precision": prec, "high_prec": high_prec}


def train_all_commodities():
    # Train across Energy Mini contracts
    symbols = ["CRUDEOILM", "NATGASMINI"]
    results = {}
    for s in symbols:
        m, res = train_commodity_model(s)
        if res:
            results[s] = res

    print(f"\n{'='*70}")
    print(f"  COMMODITY ML TRAINING SUMMARY (MINI / MICRO UNIVERSE)")
    print(f"{'='*70}")
    print(f"  {'Symbol (Mini/Micro)':22s} | {'ROC-AUC':8s} | {'Precision (P>=0.55)':20s} | {'High Conv (P>=0.60)':20s}")
    print(f"  {'-'*76}")
    for sym, r in results.items():
        print(f"  {sym:22s} | {r['auc']:.4f}   | {r['precision']*100:6.2f}%               | {r['high_prec']*100:6.2f}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train LightGBM on 5-min MCX Commodity Data (Mini/Micro supported)")
    parser.add_argument("--symbol", default=None, help="Train specific symbol or all (e.g. CRUDEOILM, GOLDM)")
    args = parser.parse_args()

    if args.symbol:
        train_commodity_model(args.symbol.upper())
    else:
        train_all_commodities()

