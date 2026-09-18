"""
ml/train_crypto.py — Walk-Forward LightGBM Training on 5-Minute Binance Perpetual Futures

Same triple-barrier labeling and LightGBM setup as ml/train_commodity.py,
applied to BTCUSDT/ETHUSDT 5-minute archives (backend/archive_crypto/).
No mini/base alias mapping is needed here -- Binance symbols are used as-is.

  - TP: +2.5R Take Profit
  - SL: -1.0R Stop Loss
  - Horizon: 12 bars (60 minutes)

Trained models are cached to cache/crypto_models/lgb_<symbol>.pkl.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
import pickle
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, precision_score, accuracy_score

from strategy.crypto_features import CRYPTO_FEATURE_COLUMNS, compute_crypto_features

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "archive_crypto"
MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "crypto_models"

CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT"]

HOLD_BARS = 12          # 60-minute forward evaluation window
TAKE_PROFIT_MULT = 2.5  # +2.5R target
STOP_MULT = 1.0         # -1.0R stop


def load_funding_df(symbol: str) -> pd.DataFrame | None:
    """Loads archive_crypto/{symbol}_funding.csv for the funding-rate features, if archived."""
    path = ARCHIVE_DIR / f"{symbol.upper()}_funding.csv"
    if not path.exists():
        return None
    fdf = pd.read_csv(path)
    fdf["timestamp"] = pd.to_datetime(fdf["timestamp"])
    return fdf


def label_crypto_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Triple-barrier target: does price hit +2.5R before -1.0R within HOLD_BARS."""
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


DEFAULT_LGB_PARAMS = {
    # Deliberately light -- a reasonable default when no ml.tune_crypto best-params
    # file exists yet. Crypto has 700k+ real rows (unlike MCX's ~4k), so running
    # ml.tune_crypto is expected to find a meaningfully larger, better-tuned model
    # via purged walk-forward Optuna search rather than staying on this default.
    "n_estimators": 20, "learning_rate": 0.08, "num_leaves": 4, "max_depth": 2,
    "min_child_samples": 150, "feature_fraction": 0.7, "bagging_fraction": 0.7,
    "bagging_freq": 1, "class_weight": "balanced", "random_state": 42, "verbosity": -1,
}


def _load_lgb_params(symbol: str) -> dict:
    """Loads ml.tune_crypto's saved best-params for this symbol, if present, else DEFAULT_LGB_PARAMS."""
    best_params_path = MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.best_params.json"
    if best_params_path.exists():
        payload = json.loads(best_params_path.read_text())
        print(f"  Using tuned hyperparameters from {best_params_path.name} "
              f"(CV AUC {payload.get('cv_auc', 0):.4f}, holdout AUC {payload.get('holdout_auc', 0):.4f})")
        return payload["params"]
    return dict(DEFAULT_LGB_PARAMS)


def train_crypto_model(symbol: str = "BTCUSDT") -> tuple[lgb.LGBMClassifier, dict]:
    """Trains a walk-forward LightGBM model on a given Binance perpetual future."""
    symbol = symbol.upper()
    csv_path = ARCHIVE_DIR / f"{symbol}_5minute.csv"
    if not csv_path.exists():
        print(f"Archive file not found: {csv_path}")
        return None, {}

    print(f"\n{'='*70}")
    print(f"  TRAINING LIGHTGBM CRYPTO MODEL: {symbol}")
    print(f"{'='*70}")

    raw_df = pd.read_csv(csv_path)
    print(f"Loaded {len(raw_df):,} 5-minute bars from {csv_path.name}")

    feat_df = compute_crypto_features(raw_df, symbol=symbol, funding_df=load_funding_df(symbol))
    labeled_df = label_crypto_bars(feat_df)
    print(f"Generated {len(labeled_df):,} labeled decision points.")

    split_idx = int(len(labeled_df) * 0.80)
    train_df = labeled_df.iloc[:split_idx]
    test_df = labeled_df.iloc[split_idx:]

    X_train = train_df[CRYPTO_FEATURE_COLUMNS]
    y_train = train_df["label"].astype(int)
    X_test = test_df[CRYPTO_FEATURE_COLUMNS]
    y_test = test_df["label"].astype(int)

    model = lgb.LGBMClassifier(**_load_lgb_params(symbol))

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False)],
    )

    preds_proba = model.predict_proba(X_test)[:, 1]
    preds_binary = (preds_proba >= 0.55).astype(int)

    auc = roc_auc_score(y_test, preds_proba)
    acc = accuracy_score(y_test, preds_binary)
    prec = precision_score(y_test, preds_binary, zero_division=0)

    high_mask = preds_proba >= 0.60
    high_prec = precision_score(y_test[high_mask], (preds_proba[high_mask] >= 0.60).astype(int), zero_division=0) if high_mask.sum() > 0 else 0.0

    print(f"\n  OUT-OF-SAMPLE TEST PERFORMANCE (Last 20% data):")
    print(f"     * ROC-AUC:               {auc:.4f}")
    print(f"     * Overall Accuracy:      {acc*100:.2f}%")
    print(f"     * Precision (P >= 0.55): {prec*100:.2f}%")
    print(f"     * Precision (P >= 0.60): {high_prec*100:.2f}% (High-Conviction)")

    imp = pd.Series(model.feature_importances_, index=CRYPTO_FEATURE_COLUMNS).sort_values(ascending=False)
    print(f"\n  TOP 5 PREDICTIVE FEATURES:")
    for f, v in imp.head(5).items():
        print(f"     - {f:24s}: {v:4d}")

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_model_path = MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.pkl"
    with open(out_model_path, "wb") as f:
        pickle.dump(model, f)
    print(f"  Model saved to: {out_model_path}")

    _write_meta(symbol, trained_through=str(labeled_df["timestamp"].max()))

    return model, {"auc": auc, "precision": prec, "high_prec": high_prec}


FINETUNE_MIN_NEW_ROWS = 100
FINETUNE_WARMUP_DAYS = 15


def _meta_path(symbol: str) -> Path:
    return MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.meta.json"


def finetune_crypto_model(symbol: str = "BTCUSDT") -> tuple[lgb.LGBMClassifier, dict]:
    """Continues training the existing saved model on data added since its last checkpoint (via init_model)."""
    symbol = symbol.upper()
    model_path = MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.pkl"
    meta_path = _meta_path(symbol)

    if not model_path.exists() or not meta_path.exists():
        print(f"  No existing model/metadata for {symbol} -- doing a full initial train instead of a fine-tune.")
        return train_crypto_model(symbol)

    with open(model_path, "rb") as f:
        existing_model = pickle.load(f)
    trained_through = json.loads(meta_path.read_text())["trained_through"]

    csv_path = ARCHIVE_DIR / f"{symbol}_5minute.csv"
    if not csv_path.exists():
        print(f"Archive file not found for {symbol}: {csv_path}")
        return None, {}

    print(f"\n{'='*70}")
    print(f"  FINE-TUNING LIGHTGBM CRYPTO MODEL: {symbol} (fine-tuning from {trained_through})")
    print(f"{'='*70}")

    raw_df = pd.read_csv(csv_path)
    raw_df["timestamp"] = pd.to_datetime(raw_df["timestamp"])
    warmup_cutoff = pd.Timestamp(trained_through) - timedelta(days=FINETUNE_WARMUP_DAYS)
    windowed_df = raw_df[raw_df["timestamp"] >= warmup_cutoff].reset_index(drop=True)

    feat_df = compute_crypto_features(windowed_df, symbol=symbol, funding_df=load_funding_df(symbol))
    labeled_df = label_crypto_bars(feat_df)

    new_df = labeled_df[labeled_df["timestamp"] > pd.Timestamp(trained_through)].reset_index(drop=True)
    if len(new_df) < FINETUNE_MIN_NEW_ROWS:
        print(f"  Only {len(new_df)} new labeled rows since {trained_through} (< {FINETUNE_MIN_NEW_ROWS}) "
              f"-- skipping fine-tune for {symbol}, model unchanged.")
        return existing_model, {}

    print(f"  {len(new_df):,} new labeled decision points since last fine-tune.")

    split_idx = int(len(new_df) * 0.80)
    train_df = new_df.iloc[:split_idx]
    test_df = new_df.iloc[split_idx:] if split_idx < len(new_df) else new_df.iloc[-max(1, len(new_df)//5):]

    X_train = train_df[CRYPTO_FEATURE_COLUMNS]
    y_train = train_df["label"].astype(int)
    X_test = test_df[CRYPTO_FEATURE_COLUMNS]
    y_test = test_df["label"].astype(int)

    if y_train.nunique() < 2:
        print(f"  New data for {symbol} is single-class (no both win/loss examples) -- skipping fine-tune.")
        return existing_model, {}

    model = lgb.LGBMClassifier(**_load_lgb_params(symbol))
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
        print(f"  Post-fine-tune eval on held-out new data: ROC-AUC {auc:.4f}, Precision(P>=0.55) {prec*100:.2f}%")

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    _write_meta(symbol, trained_through=str(new_df["timestamp"].max()))
    print(f"  Fine-tuned model saved: {model_path}")

    return model, metrics


def _write_meta(symbol: str, trained_through: str | None = None) -> None:
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"trained_through": trained_through or datetime.now().isoformat(), "symbol": symbol.upper()}
    _meta_path(symbol).write_text(json.dumps(payload, indent=2))


def finetune_all_cryptos() -> None:
    results = {}
    for s in CRYPTO_SYMBOLS:
        m, res = finetune_crypto_model(s)
        if res:
            results[s] = res

    print(f"\n{'='*70}")
    print(f"  CRYPTO ML FINE-TUNING SUMMARY")
    print(f"{'='*70}")
    if not results:
        print("  No models had enough new data to fine-tune this run.")
    for sym, r in results.items():
        print(f"  {sym:12s} | AUC {r['auc']:.4f} | Precision(P>=0.55) {r['precision']*100:6.2f}%")
    print(f"{'='*70}\n")


def train_all_cryptos():
    results = {}
    for s in CRYPTO_SYMBOLS:
        m, res = train_crypto_model(s)
        if res:
            results[s] = res

    print(f"\n{'='*70}")
    print(f"  CRYPTO ML TRAINING SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Symbol':12s} | {'ROC-AUC':8s} | {'Precision (P>=0.55)':20s} | {'High Conv (P>=0.60)':20s}")
    print(f"  {'-'*66}")
    for sym, r in results.items():
        print(f"  {sym:12s} | {r['auc']:.4f}   | {r['precision']*100:6.2f}%               | {r['high_prec']*100:6.2f}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train (or fine-tune) LightGBM on 5-min Binance Perpetual Futures Data")
    parser.add_argument("--symbol", default=None, help="Train/fine-tune specific symbol (e.g. BTCUSDT, ETHUSDT) or all")
    parser.add_argument("--finetune", action="store_true",
                         help="Fine-tune the existing saved model on new data since its last fine-tune "
                              "(via init_model) instead of a full from-scratch retrain.")
    args = parser.parse_args()

    if args.finetune:
        if args.symbol:
            finetune_crypto_model(args.symbol.upper())
        else:
            finetune_all_cryptos()
    elif args.symbol:
        train_crypto_model(args.symbol.upper())
    else:
        train_all_cryptos()
