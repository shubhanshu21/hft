"""
ml/train.py — walk-forward LightGBM training for the direction classifier.

Training is POOLED across the whole universe (symbol as a categorical
feature) — more rows than any one symbol's own history could give, and
the model can pick up cross-stock regularities instead of memorizing one
name's history.

Walk-forward = expanding window by calendar year: to predict year Y, the
model is trained only on years strictly before Y. This guarantees every
prediction used by the backtest is genuinely out-of-sample — never
trained on data from, or after, the day it's predicting.

Model capacity scales with how much training data a given fold actually
has: early folds (a couple of years of history) get the original
conservative, small-tree settings; once a fold has enough rows, a more
expressive model with early stopping (on a time-ordered validation
slice carved from the END of the training window — never random, so it
doesn't leak future rows into "training") is used instead. Early
stopping matters more here than in a one-shot training script: without
it, a wider/deeper model has more room to overfit each fold's own
training years, and there's no held-out check within the fold to catch
that before it's scored on the true test year.
"""
from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

from strategy.features import FEATURE_COLUMNS
from utils.logger import get_logger

log = get_logger(__name__)

# Small-data fallback (used when a fold doesn't have enough rows for a
# reliable validation carve-out) — same conservative settings as before.
SMALL_FOLD_PARAMS = {
    "objective": "binary", "num_leaves": 7, "min_data_in_leaf": 30, "learning_rate": 0.05,
    "n_estimators": 100, "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
    "is_unbalance": True,  # auto-balance 82%/18% class skew so longs fire in bullish markets
    "verbosity": -1,
}

# Main settings, used once a fold has enough data — regularized shallow
# trees (num_leaves 20, max_depth 5, L1/L2 penalties, column subsampling)
# prevent overfitting to financial micro-noise while boosting generalized out-of-sample alpha.
LGB_PARAMS = {
    "objective": "binary",
    "num_leaves": 20,
    "max_depth": 5,
    "min_data_in_leaf": 60,
    "learning_rate": 0.02,
    "n_estimators": 800,
    "feature_fraction": 0.70,
    "bagging_fraction": 0.80,
    "bagging_freq": 1,
    "reg_alpha": 0.20,
    "reg_lambda": 2.0,
    "is_unbalance": True,  # auto-balance 82%/18% class skew so longs fire in bullish markets
    "verbosity": -1,
}

MIN_TRAIN_ROWS = 200        # skip a fold if there isn't even this many training rows
SMALL_FOLD_CUTOFF = 2000    # below this, use SMALL_FOLD_PARAMS with no validation split
VALIDATION_FRAC = 0.15      # time-ordered TAIL of the training window, held out for early stopping
EARLY_STOPPING_ROUNDS = 30


def _compute_sample_weights(df: pd.DataFrame) -> np.ndarray:
    """Weight samples by momentum magnitude, volume flow intensity, and body expansion: focuses splits on high-alpha runners."""
    weights = np.ones(len(df))
    if "mom_pct" in df.columns and "avg_range_pct" in df.columns:
        mom_mag = np.abs(df["mom_pct"])
        denom = np.maximum(df["avg_range_pct"], 1e-4)
        weights += np.clip(mom_mag / denom, 0.0, 2.5)
    if "volume_flow_ratio" in df.columns:
        flow_mag = np.abs(df["volume_flow_ratio"])
        weights += np.clip(flow_mag * 0.4, 0.0, 1.5)
    if "body_ratio" in df.columns:
        weights += df["body_ratio"] * 0.5
    return weights.values if hasattr(weights, "values") else weights


def _fit_fold(train_df: pd.DataFrame, features: list[str], label_col: str) -> lgb.LGBMClassifier:
    if len(train_df) < SMALL_FOLD_CUTOFF:
        model = lgb.LGBMClassifier(**SMALL_FOLD_PARAMS)
        weights = _compute_sample_weights(train_df)
        model.fit(train_df[features], train_df[label_col].astype(int), sample_weight=weights)
        return model

    train_df = train_df.sort_values("date")
    split_at = int(len(train_df) * (1 - VALIDATION_FRAC))
    fit_df, val_df = train_df.iloc[:split_at], train_df.iloc[split_at:]

    fit_weights = _compute_sample_weights(fit_df)
    val_weights = _compute_sample_weights(val_df)

    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(
        fit_df[features], fit_df[label_col].astype(int),
        sample_weight=fit_weights,
        eval_set=[(val_df[features], val_df[label_col].astype(int))],
        eval_sample_weight=[val_weights],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    return model


def walk_forward_predict(df: pd.DataFrame, label_col: str = "label", extra_features: list[str] | None = None) -> pd.Series:
    """
    df must have a 'date' column, `label_col` (0/1), a 'symbol' column,
    and FEATURE_COLUMNS (plus any `extra_features` — used by the
    meta-labeling model in backtest/engine.py to also condition on the
    primary model's own confidence). Returns a Series aligned to
    df.index of out-of-sample P(up); rows with no valid training fold
    yet (the earliest calendar year, before any prior year exists to
    train on) get NaN — the backtest must skip those, not treat them as
    a signal.
    """
    df = df.copy()
    df["year"] = pd.to_datetime(df["date"]).dt.year
    df["symbol_code"] = df["symbol"].astype("category").cat.codes

    features = FEATURE_COLUMNS + ["symbol_code"] + (extra_features or [])
    predictions = pd.Series(np.nan, index=df.index)

    years = sorted(df["year"].unique())
    for test_year in years[1:]:  # first year has no prior year to train on
        train_mask = df["year"] < test_year
        test_mask = df["year"] == test_year
        train_df = df.loc[train_mask & df[label_col].notna()]
        test_df = df.loc[test_mask]

        if len(train_df) < MIN_TRAIN_ROWS or test_df.empty:
            log.info("walk_forward_predict: skipping year %s — only %d training rows.", test_year, len(train_df))
            continue

        model = _fit_fold(train_df, features, label_col)

        valid_test = test_df[features].notna().all(axis=1)
        idx = test_df.index[valid_test]
        if len(idx) == 0:
            continue
        proba_up = model.predict_proba(test_df.loc[idx, features])[:, 1]
        predictions.loc[idx] = proba_up
        log.info("walk_forward_predict: trained on %d rows (< %s), predicted %d rows for %s.", len(train_df), test_year, len(idx), test_year)

    return predictions


def feature_importance_report(df: pd.DataFrame, label_col: str = "label") -> pd.Series:
    """
    Trains one model on ALL labeled rows (not walk-forward — this is a
    diagnostic, not something used for backtest predictions) and returns
    feature importances (gain-based), sorted descending. Use this to see
    which features the model actually relies on vs. which are just
    adding noise.
    """
    df = df.copy()
    df["symbol_code"] = df["symbol"].astype("category").cat.codes
    features = FEATURE_COLUMNS + ["symbol_code"]

    labeled = df[df[label_col].notna()]
    model = lgb.LGBMClassifier(**{**LGB_PARAMS, "n_estimators": 300})
    model.fit(labeled[features], labeled[label_col].astype(int))

    importances = pd.Series(model.feature_importances_, index=features).sort_values(ascending=False)
    return importances
