"""
framework/option_chain.py — Dynamic Option Chain Builder & Strike Selector Engine.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional
import numpy as np
import pandas as pd

from .types import OptionType, StrikeMode
from .models import OptionContract, OptionChainSnapshot, Greeks
from .greeks import compute_greeks, solve_implied_volatility


class OptionChain:
    """
    Container and query manager for live or historical option chains.
    """

    def __init__(self, underlying_symbol: str, underlying_price: float, expiry: str):
        self.underlying_symbol = underlying_symbol
        self.underlying_price = underlying_price
        self.expiry = expiry
        self.contracts: dict[float, dict[OptionType, OptionContract]] = {}

    def add_contract(self, contract: OptionContract) -> None:
        if contract.strike not in self.contracts:
            self.contracts[contract.strike] = {}
        self.contracts[contract.strike][contract.option_type] = contract

    @property
    def strikes(self) -> list[float]:
        return sorted(self.contracts.keys())

    def get_atm_strike(self, strike_step: Optional[float] = None) -> float:
        available = self.strikes
        if not available:
            if strike_step and strike_step > 0:
                return round(self.underlying_price / strike_step) * strike_step
            return self.underlying_price
        return min(available, key=lambda s: abs(s - self.underlying_price))

    def select_strike(
        self,
        option_type: OptionType,
        mode: StrikeMode = StrikeMode.ATM,
        offset: int = 0,
        target_delta: Optional[float] = None,
        strike_step: Optional[float] = None,
    ) -> Optional[OptionContract]:
        """
        Selects an option contract based on StrikeMode, offset (0=ATM, 1=OTM1, -1=ITM1), or Delta target.
        """
        strikes = self.strikes
        if not strikes:
            return None

        atm_strike = self.get_atm_strike(strike_step)
        atm_idx = strikes.index(atm_strike) if atm_strike in strikes else 0

        # Delta target search
        if mode == StrikeMode.DELTA_TARGET and target_delta is not None:
            best_contract = None
            best_diff = 999.0
            for s in strikes:
                c = self.contracts.get(s, {}).get(option_type)
                if c and c.greeks:
                    diff = abs(abs(c.greeks.delta) - target_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best_contract = c
            if best_contract:
                return best_contract

        # Offset-based selection
        # For Calls: Higher strike = OTM (+1), Lower strike = ITM (-1)
        # For Puts: Lower strike = OTM (+1), Higher strike = ITM (-1)
        if option_type in (OptionType.CALL, "CE"):
            target_idx = atm_idx + offset
        else:
            target_idx = atm_idx - offset

        target_idx = max(0, min(len(strikes) - 1, target_idx))
        target_strike = strikes[target_idx]
        return self.contracts.get(target_strike, {}).get(option_type)

    def calculate_pcr(self) -> tuple[float, float]:
        """Returns (pcr_open_interest, pcr_volume)."""
        call_oi, put_oi = 0.0, 0.0
        call_vol, put_vol = 0.0, 0.0

        for strike, opts in self.contracts.items():
            if OptionType.CALL in opts:
                c = opts[OptionType.CALL]
                call_oi += c.oi or 0.0
                call_vol += c.volume or 0.0
            if OptionType.PUT in opts:
                p = opts[OptionType.PUT]
                put_oi += p.oi or 0.0
                put_vol += p.volume or 0.0

        pcr_oi = (put_oi / call_oi) if call_oi > 0 else 1.0
        pcr_vol = (put_vol / call_vol) if call_vol > 0 else 1.0
        return round(pcr_oi, 3), round(pcr_vol, 3)

    def calculate_max_pain(self) -> float:
        """Calculates the strike price where option sellers lose the least amount of money at expiry."""
        strikes = self.strikes
        if not strikes:
            return self.underlying_price

        total_loss_per_strike = {}
        for test_strike in strikes:
            total_loss = 0.0
            for strike, opts in self.contracts.items():
                if OptionType.CALL in opts:
                    c = opts[OptionType.CALL]
                    total_loss += max(0.0, test_strike - strike) * (c.oi or 0.0)
                if OptionType.PUT in opts:
                    p = opts[OptionType.PUT]
                    total_loss += max(0.0, strike - test_strike) * (p.oi or 0.0)
            total_loss_per_strike[test_strike] = total_loss

        return min(total_loss_per_strike, key=total_loss_per_strike.get)
