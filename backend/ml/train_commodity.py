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
import json
from datetime import datetime, timedelta
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
TAKE_PROFIT_MULT = 2.5  # +2.5R target (deliberately more extreme than the strategy's own 1.20R TP -- see label_commodity_bars docstring)
STOP_MULT = 1.0         # -1.0R stop


def label_commodity_bars(df: pd.DataFrame, is_natgas: bool = False) -> pd.DataFrame:
    """
    Labels each bar with a naive fixed-barrier target: does price hit
    +2.5R before -1.0R within HOLD_BARS (a shorter, easier-to-separate
    pattern than the strategy's actual exit rules).

    A version of this function that instead simulated the ACTUAL live/
    backtest exit rules bar-by-bar (SL/TP/breakeven-arm/trail/timeout) was
    tried and reverted on 2026-09-10: it's the conceptually more correct
    target (it's what the strategy actually trades), but on the current
    ~4k-row REAL archive it produces a severely imbalanced label (~65-74%
    "win", since breakeven-lock protects most trades into a small positive
    close) that the model can't learn from (best AUC ~0.58-0.60 after
    sweeping win-size thresholds, vs ~0.72/0.67 here). Revisit once the
    real archive (grows ~130 rows/day via the daily top-up job) is large
    enough for that harder, more balanced-near-noise target to be learnable
    -- see conversation/git history for the full threshold sweep.
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
    is_natgas = "NATGAS" in symbol.upper() or "NATURALGAS" in symbol.upper()
    feat_df = compute_commodity_features(raw_df, symbol=base_sym)
    labeled_df = label_commodity_bars(feat_df, is_natgas=is_natgas)
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
        # Deliberately light -- backtested 2026-09-10: the old 150-tree/31-leaf
        # config (tuned for the 212k-row FAKE archive) massively overfits the
        # real ~4k-row archive (CRUDEOILM test AUC 0.52, near-random). This
        # lighter config recovers AUC ~0.72 on the same real data. Revisit
        # once the real archive is much larger (the daily top-up job grows
        # it ~130 rows/day) -- more data can support more tree capacity again.
        n_estimators=20,
        learning_rate=0.08,
        num_leaves=4,
        max_depth=2,
        min_child_samples=150,
        feature_fraction=0.7,
        bagging_fraction=0.7,
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

    # Checkpoint the last data timestamp actually used -- NOT wall-clock
    # "now" -- so a later fine-tune knows exactly which rows are new. Real
    # archive data usually lags behind the current date (it's topped up
    # periodically, not continuously), so "now" would be wrong here.
    _write_meta(symbol, trained_through=str(labeled_df["timestamp"].max()))

    return model, {"auc": auc, "precision": prec, "high_prec": high_prec}


FINETUNE_MIN_NEW_ROWS = 100    # skip the fine-tune if fewer than this many fresh labeled rows exist
FINETUNE_WARMUP_DAYS = 15      # extra history fed into feature computation before the cutoff, purely so
                              # rolling indicators (EMA/ADX/ATR) are warmed up at the start of the new
                              # slice -- these warmup rows are never themselves used to fit the model.


def _meta_path(symbol: str) -> Path:
    return MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.meta.json"


def finetune_commodity_model(symbol: str = "CRUDEOILM") -> tuple[lgb.LGBMClassifier, dict]:
    """
    Continues training the EXISTING saved model on only the data added since
    its last fine-tune (via LightGBM's init_model -- adds boosting rounds on top
    of the existing trees) instead of retraining from scratch on the full
    archive every time. Falls back to a full train_commodity_model() the
    first time a symbol has no saved model/metadata yet.
    """
    base_sym = COMMODITY_ALIASES.get(symbol.upper(), symbol.upper())
    model_path = MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.pkl"
    meta_path = _meta_path(symbol)

    if not model_path.exists() or not meta_path.exists():
        print(f"  No existing model/metadata for {symbol.upper()} -- doing a full initial train instead of a fine-tune.")
        # train_commodity_model() writes the metadata checkpoint itself (with
        # the correct last-data-timestamp, not wall-clock "now") on success.
        return train_commodity_model(symbol)

    with open(model_path, "rb") as f:
        existing_model = pickle.load(f)
    trained_through = json.loads(meta_path.read_text())["trained_through"]

    csv_path = ARCHIVE_DIR / f"{base_sym}_5minute.csv"
    if not csv_path.exists():
        csv_path = ARCHIVE_DIR / f"{symbol.upper()}_5minute.csv"
    if not csv_path.exists():
        print(f"Archive file not found for {symbol.upper()}: {csv_path}")
        return None, {}

    print(f"\n{'='*70}")
    print(f"  FINE-TUNING LIGHTGBM COMMODITY MODEL: {symbol.upper()} (fine-tuning from {trained_through})")
    print(f"{'='*70}")

    raw_df = pd.read_csv(csv_path)
    raw_df["timestamp"] = pd.to_datetime(raw_df["timestamp"])
    warmup_cutoff = pd.Timestamp(trained_through) - timedelta(days=FINETUNE_WARMUP_DAYS)
    windowed_df = raw_df[raw_df["timestamp"] >= warmup_cutoff].reset_index(drop=True)

    is_natgas = "NATGAS" in symbol.upper() or "NATURALGAS" in symbol.upper()
    feat_df = compute_commodity_features(windowed_df, symbol=base_sym)
    labeled_df = label_commodity_bars(feat_df, is_natgas=is_natgas)

    # Only the rows strictly after the last fine-tune are "new" -- the warmup
    # rows before that exist solely to give rolling features valid history.
    new_df = labeled_df[labeled_df["timestamp"] > pd.Timestamp(trained_through)].reset_index(drop=True)
    if len(new_df) < FINETUNE_MIN_NEW_ROWS:
        print(f"  Only {len(new_df)} new labeled rows since {trained_through} (< {FINETUNE_MIN_NEW_ROWS}) "
              f"-- skipping fine-tune for {symbol.upper()}, model unchanged.")
        return existing_model, {}

    print(f"  {len(new_df):,} new labeled decision points since last fine-tune.")

    split_idx = int(len(new_df) * 0.80)
    train_df = new_df.iloc[:split_idx]
    test_df  = new_df.iloc[split_idx:] if split_idx < len(new_df) else new_df.iloc[-max(1, len(new_df)//5):]

    X_train = train_df[COMMODITY_FEATURE_COLUMNS]
    y_train = train_df["label"].astype(int)
    X_test  = test_df[COMMODITY_FEATURE_COLUMNS]
    y_test  = test_df["label"].astype(int)

    if y_train.nunique() < 2:
        print(f"  New data for {symbol.upper()} is single-class (no both win/loss examples) -- skipping fine-tune.")
        return existing_model, {}

    # Same hyperparameters as a fresh train -- init_model is what makes this
    # a continuation (added boosting rounds on top of existing_model's trees)
    # rather than a from-scratch fit.
    model = lgb.LGBMClassifier(
        # Deliberately light -- backtested 2026-09-10: the old 150-tree/31-leaf
        # config (tuned for the 212k-row FAKE archive) massively overfits the
        # real ~4k-row archive (CRUDEOILM test AUC 0.52, near-random). This
        # lighter config recovers AUC ~0.72 on the same real data. Revisit
        # once the real archive is much larger (the daily top-up job grows
        # it ~130 rows/day) -- more data can support more tree capacity again.
        n_estimators=20,
        learning_rate=0.08,
        num_leaves=4,
        max_depth=2,
        min_child_samples=150,
        feature_fraction=0.7,
        bagging_fraction=0.7,
        bagging_freq=1,
        class_weight="balanced",
        random_state=42,
        verbosity=-1,
    )
    fit_kwargs = {"eval_set": [(X_test, y_test)]} if len(X_test) > 0 else {}
    model.fit(
        X_train, y_train,
        init_model=existing_model,
        **fit_kwargs,
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)] if fit_kwargs else [],
    )

    metrics: dict = {}
    if len(X_test) > 0 and y_test.nunique() > 1:
        preds_proba = model.predict_proba(X_test)[:, 1]
        preds_binary = (preds_proba >= 0.55).astype(int)
        auc = roc_auc_score(y_test, preds_proba)
        prec = precision_score(y_test, preds_binary, zero_division=0)
        metrics = {"auc": auc, "precision": prec}
        print(f"  📊 Post-fine-tune eval on held-out new data: ROC-AUC {auc:.4f}, Precision(P>=0.55) {prec*100:.2f}%")

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for save_name in {symbol.lower(), base_sym.lower()}:
        with open(MODEL_CACHE_DIR / f"lgb_{save_name}.pkl", "wb") as f:
            pickle.dump(model, f)
    _write_meta(symbol, trained_through=str(new_df["timestamp"].max()))
    print(f"  💾 Fine-tuned model saved: {model_path}")

    return model, metrics


def _write_meta(symbol: str, trained_through: str | None = None) -> None:
    """Records the last data timestamp actually used to train/fine-tune, so a later fine-tune knows which rows are new. Every caller passes trained_through explicitly (from the labeled data itself); the wall-clock fallback here is only a defensive default, never rely on it."""
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"trained_through": trained_through or datetime.now().isoformat(), "symbol": symbol.upper()}
    for save_name in {symbol.lower(), COMMODITY_ALIASES.get(symbol.upper(), symbol.upper()).lower()}:
        _meta_path(save_name).write_text(json.dumps(payload, indent=2))


def finetune_all_commodities() -> None:
    symbols = ["CRUDEOILM", "NATGASMINI"]
    results = {}
    for s in symbols:
        m, res = finetune_commodity_model(s)
        if res:
            results[s] = res

    print(f"\n{'='*70}")
    print(f"  COMMODITY ML FINE-TUNING SUMMARY (MINI / MICRO UNIVERSE)")
    print(f"{'='*70}")
    if not results:
        print("  No models had enough new data to fine-tune this run.")
    for sym, r in results.items():
        print(f"  {sym:22s} | AUC {r['auc']:.4f} | Precision(P>=0.55) {r['precision']*100:6.2f}%")
    print(f"{'='*70}\n")


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
    parser = argparse.ArgumentParser(description="Train (or fine-tune) LightGBM on 5-min MCX Commodity Data (Mini/Micro supported)")
    parser.add_argument("--symbol", default=None, help="Train/fine-tune specific symbol or all (e.g. CRUDEOILM, GOLDM)")
    parser.add_argument("--finetune", action="store_true",
                         help="Fine-tune the existing saved model on new data since its last fine-tune "
                              "(via init_model) instead of a full from-scratch retrain.")
    args = parser.parse_args()

    # train_commodity_model()/finetune_commodity_model() write their own
    # metadata checkpoint (last data timestamp used) on success -- no extra
    # bookkeeping needed here.
    if args.finetune:
        if args.symbol:
            finetune_commodity_model(args.symbol.upper())
        else:
            finetune_all_commodities()
    elif args.symbol:
        train_commodity_model(args.symbol.upper())
    else:
        train_all_commodities()

