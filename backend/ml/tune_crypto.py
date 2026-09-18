"""
ml/tune_crypto.py — Purged Walk-Forward Optuna Hyperparameter Search (Crypto)

The MCX LightGBM config in ml/train_commodity.py was deliberately kept tiny
(n_estimators=20, num_leaves=4, max_depth=2) because that pipeline only had
~4k real rows to train on -- a bigger model just overfit. The crypto archive
has 700k+ real 5-minute rows (7 years of BTCUSDT/ETHUSDT), which can support
a properly tuned, larger model -- reusing the MCX config here would leave
most of that data's signal on the table. This module tunes LightGBM
hyperparameters specifically for crypto via Optuna, instead of hand-copying
MCX's numbers.

Naive K-fold / random-split CV leaks information here: labels are formed by
looking up to HOLD_BARS bars into the future (triple-barrier), so a fold
boundary placed inside that window lets test-set outcome information bleed
into adjacent training rows. This implements Lopez de Prado-style purged/
embargoed walk-forward CV (embargo width = the label horizon) so the
hyperparameter search is scored honestly, and holds out a final untouched
15% of history to sanity-check the tuned model outside the search entirely.

Usage:
    python3 -m ml.tune_crypto --symbol BTCUSDT --trials 40
    python3 -m ml.tune_crypto --trials 40          # both BTCUSDT and ETHUSDT
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import lightgbm as lgb
import optuna
from optuna.samplers import TPESampler
from sklearn.metrics import roc_auc_score

from strategy.crypto_features import CRYPTO_FEATURE_COLUMNS, compute_crypto_features
from ml.train_crypto import label_crypto_bars, load_funding_df, HOLD_BARS, ARCHIVE_DIR, MODEL_CACHE_DIR

optuna.logging.set_verbosity(optuna.logging.WARNING)

N_SPLITS = 4
EMBARGO_BARS = HOLD_BARS  # must be >= the forward-looking label horizon to avoid leakage


def purged_walk_forward_splits(n: int, n_splits: int = N_SPLITS, embargo: int = EMBARGO_BARS):
    """Anchored walk-forward folds with an embargo gap around each train/test boundary."""
    fold_size = n // (n_splits + 1)
    for k in range(1, n_splits + 1):
        train_end = k * fold_size
        test_start = min(train_end + embargo, n)
        test_end = min(test_start + fold_size, n)
        if test_start >= test_end or train_end - embargo <= 0:
            continue
        train_idx = np.arange(0, max(0, train_end - embargo))
        test_idx = np.arange(test_start, test_end)
        yield train_idx, test_idx


def _objective(trial: "optuna.Trial", X: pd.DataFrame, y: pd.Series) -> float:
    params = {
        "n_estimators": trial.suggest_int("n_estimators", 50, 400),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 4, 96),
        "max_depth": trial.suggest_int("max_depth", 2, 9),
        "min_child_samples": trial.suggest_int("min_child_samples", 20, 500),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "bagging_freq": trial.suggest_int("bagging_freq", 1, 7),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
        "class_weight": "balanced",
        "random_state": 42,
        "verbosity": -1,
        "n_jobs": -1,
    }

    aucs = []
    for train_idx, test_idx in purged_walk_forward_splits(len(X)):
        X_train, y_train = X.iloc[train_idx], y.iloc[train_idx]
        X_test, y_test = X.iloc[test_idx], y.iloc[test_idx]
        if y_train.nunique() < 2 or y_test.nunique() < 2:
            continue
        model = lgb.LGBMClassifier(**params)
        model.fit(X_train, y_train)
        preds = model.predict_proba(X_test)[:, 1]
        aucs.append(roc_auc_score(y_test, preds))

    if not aucs:
        return 0.5
    return float(np.mean(aucs))


def tune_crypto_model(symbol: str = "BTCUSDT", n_trials: int = 40) -> dict:
    symbol = symbol.upper()
    csv_path = ARCHIVE_DIR / f"{symbol}_5minute.csv"
    if not csv_path.exists():
        print(f"Archive file not found: {csv_path}")
        return {}

    print(f"\n{'='*70}")
    print(f"  PURGED WALK-FORWARD OPTUNA TUNING: {symbol} ({n_trials} trials, {N_SPLITS} folds)")
    print(f"{'='*70}")

    raw_df = pd.read_csv(csv_path)
    feat_df = compute_crypto_features(raw_df, symbol=symbol, funding_df=load_funding_df(symbol))
    labeled_df = label_crypto_bars(feat_df)
    print(f"Loaded {len(raw_df):,} bars -> {len(labeled_df):,} labeled decision points.")

    # Final 15% is never touched by the search -- an honest, un-tuned sanity check.
    holdout_idx = int(len(labeled_df) * 0.85)
    tune_df = labeled_df.iloc[:holdout_idx].reset_index(drop=True)
    holdout_df = labeled_df.iloc[holdout_idx:].reset_index(drop=True)

    X = tune_df[CRYPTO_FEATURE_COLUMNS]
    y = tune_df["label"].astype(int)

    study = optuna.create_study(direction="maximize", sampler=TPESampler(seed=42))
    study.optimize(lambda t: _objective(t, X, y), n_trials=n_trials, show_progress_bar=False)

    best_params = dict(study.best_params)
    best_params.update({"class_weight": "balanced", "random_state": 42, "verbosity": -1})
    print(f"\n  Best purged walk-forward CV AUC: {study.best_value:.4f}")
    print(f"  Best params: {best_params}")

    final_model = lgb.LGBMClassifier(**best_params)
    final_model.fit(X, y)
    X_hold = holdout_df[CRYPTO_FEATURE_COLUMNS]
    y_hold = holdout_df["label"].astype(int)
    hold_auc = roc_auc_score(y_hold, final_model.predict_proba(X_hold)[:, 1])
    print(f"  Holdout AUC (untouched by tuning, most recent 15% of history): {hold_auc:.4f}")

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    params_path = MODEL_CACHE_DIR / f"lgb_{symbol.lower()}.best_params.json"
    params_path.write_text(json.dumps(
        {"params": best_params, "cv_auc": study.best_value, "holdout_auc": hold_auc},
        indent=2,
    ))
    print(f"  Saved best params to: {params_path}")

    return best_params


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purged walk-forward Optuna hyperparameter tuning for crypto LightGBM models")
    parser.add_argument("--symbol", default=None, help="Symbol to tune (e.g. BTCUSDT). Default: both BTCUSDT and ETHUSDT")
    parser.add_argument("--trials", type=int, default=40, help="Number of Optuna trials")
    args = parser.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else ["BTCUSDT", "ETHUSDT"]
    for s in symbols:
        tune_crypto_model(s, n_trials=args.trials)
