"""
ml/train_equity.py -- Walk-Forward LightGBM Training on the pooled NIFTY50 dataset.

Rebuilt 2026-09-19 alongside the rest of the equity-scalper rebuild (see
strategy/equity_universe.py's docstring). Mirrors ml/train_commodity.py's
triple-barrier labeling shape, but with one deliberate structural
difference: ONE model is trained on rows POOLED across all 49 NIFTY50
symbols, not one model per stock. This matches backtest_equity.py's own
"one shared rule set for the whole universe, not per-symbol tuning" design
(see that module's docstring for why per-symbol tuning was the original
version's core flaw) -- a per-stock model would reopen exactly that
selection-bias risk (a stock's own model could quietly curve-fit to that
stock's quirks). Pooling also gives a genuinely large training set (millions
of rows) that no single MCX/currency symbol has ever had.

Trained ONLY on the TRAIN window already established for backtest_equity.py's
own train/test split (2022-08-01 to 2025-06-30) -- the three held-out TEST
folds used to validate the rule-based thresholds must stay completely
unseen by this model, or comparing "rules alone" vs "rules + ML filter"
would be comparing apples to a fitted orange.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, precision_score, accuracy_score

from strategy.equity_features import EQUITY_FEATURE_COLUMNS, compute_equity_features
from strategy.equity_universe import NIFTY50_SYMBOLS

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "archive_equity"
MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "equity_models"

HOLD_BARS = 12
TAKE_PROFIT_MULT = 2.5
STOP_MULT = 1.0

TRAIN_FROM = "2022-08-01"
TRAIN_TO = "2025-06-30"  # matches backtest_equity.py's established train/test split -- see module docstring


def label_equity_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Same fixed-barrier labeling shape as label_commodity_bars: does price
    move +2.5R before -1.0R within HOLD_BARS. A proxy target for "was this a
    good bar to go long" -- independent of the live strategy's own dynamic
    trailing exit mechanics, same relationship commodity's model already has
    to its own (different) live exit rules."""
    df = df.copy()
    n = len(df)
    labels = np.full(n, np.nan)
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values

    for i in range(n - HOLD_BARS):
        entry = closes[i]
        atr = atrs[i]
        sdist = max(1.4 * atr, 0.005 * entry)
        if sdist <= 0 or entry <= 0:
            continue
        tp = entry + TAKE_PROFIT_MULT * sdist
        sl = entry - STOP_MULT * sdist
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


def train_equity_model() -> tuple[lgb.LGBMClassifier, dict]:
    print(f"\n{'='*70}")
    print(f"  TRAINING LIGHTGBM EQUITY MODEL (pooled NIFTY50, {TRAIN_FROM} to {TRAIN_TO})")
    print(f"{'='*70}")

    pooled = []
    for sym in NIFTY50_SYMBOLS:
        p = ARCHIVE_DIR / f"{sym.upper()}_5minute.csv"
        if not p.exists():
            continue
        raw_df = pd.read_csv(p)
        raw_df = raw_df[(raw_df["timestamp"] >= TRAIN_FROM) & (raw_df["timestamp"] <= TRAIN_TO + "T23:59:59")]
        if len(raw_df) < 100:
            continue
        feat_df = compute_equity_features(raw_df.reset_index(drop=True))
        labeled_df = label_equity_bars(feat_df)
        labeled_df["symbol"] = sym
        pooled.append(labeled_df)
    all_df = pd.concat(pooled, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    print(f"Pooled {len(all_df):,} labeled decision points from {len(pooled)} symbols.")
    print(f"Label balance: {all_df['label'].mean()*100:.1f}% positive")

    # Chronological 80/20 split WITHIN the train window only -- the three
    # held-out TEST folds already used to validate the rule-based thresholds
    # are outside [TRAIN_FROM, TRAIN_TO] entirely and never touched here.
    split_idx = int(len(all_df) * 0.80)
    train_df = all_df.iloc[:split_idx]
    val_df = all_df.iloc[split_idx:]

    X_train = train_df[EQUITY_FEATURE_COLUMNS]
    y_train = train_df["label"].astype(int)
    X_val = val_df[EQUITY_FEATURE_COLUMNS]
    y_val = val_df["label"].astype(int)

    model = lgb.LGBMClassifier(
        # Much larger dataset than any single commodity ever had (millions vs
        # thousands of rows) -- more tree capacity than commodity's
        # deliberately-starved config is appropriate here, per that module's
        # own comment ("more data can support more tree capacity again").
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        max_depth=6,
        min_child_samples=200,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        class_weight="balanced",
        random_state=42,
        verbosity=-1,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )

    preds_proba = model.predict_proba(X_val)[:, 1]
    preds_binary = (preds_proba >= 0.55).astype(int)
    auc = roc_auc_score(y_val, preds_proba)
    acc = accuracy_score(y_val, preds_binary)
    prec = precision_score(y_val, preds_binary, zero_division=0)
    high_mask = preds_proba >= 0.60
    high_prec = precision_score(y_val[high_mask], (preds_proba[high_mask] >= 0.60).astype(int), zero_division=0) if high_mask.sum() > 0 else 0.0

    print(f"\n  Out-of-sample validation performance (last 20% of TRAIN window only):")
    print(f"     ROC-AUC:               {auc:.4f}")
    print(f"     Overall Accuracy:      {acc*100:.2f}%")
    print(f"     Precision (P>=0.55):   {prec*100:.2f}%")
    print(f"     Precision (P>=0.60):   {high_prec*100:.2f}% (high-conviction)")

    imp = pd.Series(model.feature_importances_, index=EQUITY_FEATURE_COLUMNS).sort_values(ascending=False)
    print(f"\n  Top 5 predictive features:")
    for f, v in imp.head(5).items():
        print(f"     - {f:24s}: {v:4d}")

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MODEL_CACHE_DIR / "lgb_equity_pooled.pkl"
    with open(out_path, "wb") as f:
        pickle.dump(model, f)
    print(f"\n  Model saved to: {out_path}")

    return model, {"auc": auc, "precision": prec, "high_prec": high_prec}


if __name__ == "__main__":
    train_equity_model()
