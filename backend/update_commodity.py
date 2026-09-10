#!/usr/bin/env python3
"""
update_commodity.py — CLI: Top up MCX Commodity Archives (REAL data via Upstox) & Retrain ML Models

Usage:
    python3 update_commodity.py           # Update archives + retrain models
    python3 update_commodity.py --no-train # Update archives only

NOTE: this does a FULL from-scratch retrain (ml.train_commodity.train_all_commodities),
not the incremental fine-tune used by the scheduled weekly job -- see
ml/weekly_finetune.py (real_commodity_data top-up + finetune_all_commodities)
for that. Archives are fetched via real_commodity_data.py (genuine Upstox
history) -- this used to call download_commodity_data.py's synthetic
random-walk generator; that module still exists but is no longer used here
as of 2026-09-10, since its data was never real market history.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_commodity_data import build_all_real_commodity_archives
from ml.train_commodity import train_all_commodities


def main():
    parser = argparse.ArgumentParser(description="Update MCX Commodity Data Archives (real, via Upstox) & Retrain ML Models")
    parser.add_argument("--no-train", action="store_true", help="Skip model retraining after downloading archives")
    args = parser.parse_args()

    print("\n" + "="*75)
    print("  MCX COMMODITY PIPELINE UPDATE")
    print("="*75)

    # 1. Update Archives (real data, incremental top-up)
    print("\n[Step 1/2] Updating Commodity Archives (real Upstox data)...")
    build_all_real_commodity_archives(topup=True)

    # 2. Retrain ML Models
    if not args.no_train:
        print("\n[Step 2/2] Retraining LightGBM Machine Learning Models...")
        train_all_commodities()
    else:
        print("\n[Step 2/2] Skipped ML retraining (--no-train requested).")

    print("\n" + "="*75)
    print("  ✅ All Commodity Datasets & Models are Up-to-Date!")
    print("="*75 + "\n")


if __name__ == "__main__":
    main()
