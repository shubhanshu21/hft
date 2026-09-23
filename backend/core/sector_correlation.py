"""
core/sector_correlation.py — Sector mapping and cross-asset correlation risk gates.

Prevents portfolio concentration by capping simultaneous entries in correlated
names (e.g. taking 3 IT stocks or 3 Banking stocks simultaneously during a sector dump)
and provides cross-market correlation limits.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Sector classification for NIFTY 50 universe
NIFTY_SECTOR_MAP: Dict[str, str] = {
    # Banking & Financial Services
    "HDFCBANK": "BANKING_FINANCE",
    "ICICIBANK": "BANKING_FINANCE",
    "SBIN": "BANKING_FINANCE",
    "KOTAKBANK": "BANKING_FINANCE",
    "AXISBANK": "BANKING_FINANCE",
    "INDUSINDBK": "BANKING_FINANCE",
    "BAJFINANCE": "BANKING_FINANCE",
    "BAJAJFINSV": "BANKING_FINANCE",
    "HDFCLIFE": "BANKING_FINANCE",
    "SBILIFE": "BANKING_FINANCE",

    # Information Technology
    "TCS": "IT",
    "INFY": "IT",
    "HCLTECH": "IT",
    "WIPRO": "IT",
    "TECHM": "IT",

    # Automobile & Auto Ancillary
    "MARUTI": "AUTO",
    "M&M": "AUTO",
    "EICHERMOT": "AUTO",
    "HEROMOTOCO": "AUTO",
    "BAJAJ-AUTO": "AUTO",

    # Oil, Gas & Consumable Fuels / Energy
    "RELIANCE": "ENERGY",
    "ONGC": "ENERGY",
    "BPCL": "ENERGY",
    "IOC": "ENERGY",
    "COALINDIA": "ENERGY",
    "NTPC": "POWER",
    "POWERGRID": "POWER",

    # Metals & Mining
    "TATASTEEL": "METALS",
    "JSWSTEEL": "METALS",
    "HINDALCO": "METALS",

    # Pharmaceuticals & Healthcare
    "SUNPHARMA": "PHARMA",
    "DRREDDY": "PHARMA",
    "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA",
    "APOLLOHOSP": "HEALTHCARE",

    # FMCG & Consumer Goods
    "HINDUNILVR": "FMCG",
    "ITC": "FMCG",
    "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG",
    "TITAN": "CONSUMER_DURABLES",
    "ASIANPAINT": "PAINTS",

    # Construction Materials / Cement
    "ULTRACEMCO": "CEMENT",
    "GRASIM": "CEMENT",
    "SHREECEM": "CEMENT",

    # Capital Goods, Infrastructure & Logistics
    "LT": "INFRA",
    "ADANIENT": "CONGLOMERATE",
    "ADANIPORTS": "LOGISTICS",
    "BHARTIARTL": "TELECOM",
    "UPL": "CHEMICALS",
}

# Default maximum concurrent open positions allowed per sector
DEFAULT_MAX_PER_SECTOR: int = 1


class SectorCorrelationGate:
    """
    Evaluates whether a new candidate trade violates sector concentration
    or cross-asset correlation rules against current active positions.
    """

    def __init__(self, max_per_sector: int = DEFAULT_MAX_PER_SECTOR) -> None:
        self.max_per_sector = max_per_sector

    @staticmethod
    def get_sector(symbol: str) -> str:
        """Returns the sector for a symbol, or 'OTHER' / 'COMMODITY' / 'CURRENCY' if not mapped."""
        sym_clean = symbol.upper().strip()
        if sym_clean in NIFTY_SECTOR_MAP:
            return NIFTY_SECTOR_MAP[sym_clean]
        if any(c in sym_clean for c in ["CRUDE", "GOLD", "SILVER", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD"]):
            return "COMMODITY"
        if any(cur in sym_clean for cur in ["USDINR", "EURINR", "GBPINR", "JPYINR"]):
            return "CURRENCY"
        if any(idx in sym_clean for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY"]):
            return "INDEX_DERIVATIVES"
        return "OTHER"

    def can_enter(
        self,
        candidate_symbol: str,
        active_positions: List[Dict] | Set[str] | List[str],
        max_per_sector: Optional[int] = None,
    ) -> tuple[bool, str]:
        """
        Determines if candidate_symbol can be entered without exceeding sector concentration limits.

        Returns:
            (allowed: bool, reason: str)
        """
        limit = max_per_sector if max_per_sector is not None else self.max_per_sector
        candidate_sector = self.get_sector(candidate_symbol)

        # Extract active symbol strings
        active_syms: List[str] = []
        for pos in active_positions:
            if isinstance(pos, dict):
                sym = pos.get("symbol")
                if sym:
                    active_syms.append(str(sym))
            elif isinstance(pos, str):
                active_syms.append(pos)

        # Count how many current positions share candidate's sector
        sector_counts: Dict[str, int] = {}
        for sym in active_syms:
            sec = self.get_sector(sym)
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

        current_in_sector = sector_counts.get(candidate_sector, 0)
        if current_in_sector >= limit:
            reason = (
                f"Sector cap reached: {candidate_symbol} ({candidate_sector}) has {current_in_sector} "
                f"active position(s), limit is {limit}."
            )
            log.info("🚫 %s", reason)
            return False, reason

        return True, "OK"
