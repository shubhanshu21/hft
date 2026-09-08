#!/usr/bin/env python3
"""
update_commodity.py — CLI: Top up MCX Commodity 5-minute Archives & Retrain ML Models

Usage:
    python3 update_commodity.py           # Update archives + retrain models
    python3 update_commodity.py --no-train # Update archives only
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_commodity_data import build_all_commodity_archives
from ml.train_commodity import train_all_commodities


def main():
    parser = argparse.ArgumentParser(description="Update MCX Commodity Data Archives & Retrain ML Models")
    parser.add_argument("--no-train", action="store_true", help="Skip model retraining after downloading archives")
    args = parser.parse_args()

    print("\n" + "="*75)
    print("  MCX COMMODITY PIPELINE UPDATE")
    print("="*75)

    # 1. Update Archives
    print("\n[Step 1/2] Updating 5-Minute Commodity Archives...")
    build_all_commodity_archives()

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
