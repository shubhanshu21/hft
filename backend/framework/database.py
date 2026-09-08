"""
framework/database.py — Unified SQLite Database Engine for Multi-Asset Trading.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Connect to core database
sys.path.insert(0, str(Path(__file__).parent.parent))
from database import TradingDB

__all__ = ["TradingDB"]
