"""
framework/config.py — 100% Configurable Settings Engine for Multi-Asset Trading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any
import json
import os


@dataclass
class StatutoryCostConfig:
    # Brokerage
    brokerage_flat_cap: float = 20.0
    brokerage_pct: float = 0.05
    # STT / CTT
    equity_intraday_stt_sell_pct: float = 0.025
    equity_delivery_stt_both_pct: float = 0.10
    futures_ctt_sell_pct: float = 0.01
    options_stt_sell_premium_pct: float = 0.0625
    options_exercise_stt_pct: float = 0.125
    # Stamp Duty
    stamp_duty_equity_buy_pct: float = 0.003
    stamp_duty_futures_buy_pct: float = 0.002
    stamp_duty_options_buy_pct: float = 0.003
    # Exchange Turnover & SEBI Fees
    nse_cash_exchange_fee_pct: float = 0.00325
    nse_futures_exchange_fee_pct: float = 0.0019
    nse_options_exchange_fee_pct: float = 0.05
    mcx_futures_exchange_fee_pct: float = 0.0021
    sebi_fee_per_side_pct: float = 0.0001
    # GST
    gst_rate_pct: float = 18.0
    # Slippage Buffer
    slippage_ticks: float = 0.5


@dataclass
class RiskBudgetConfig:
    initial_capital: float = 100_000.0
    risk_pct_per_trade: float = 2.5
    default_leverage: float = 4.0
    max_open_positions: int = 5
    max_daily_loss_pct: float = 5.0
    profit_target_r_mult: float = 1.20
    stop_loss_vol_mult: float = 1.4
    breakeven_trigger_r_mult: float = 0.35
    breakeven_lock_pct: float = 0.0008
    trailing_stop_dist_mult: float = 0.30
    max_hold_bars: int = 16


@dataclass
class SessionConfig:
    # IST 24-hour minute offsets (from 00:00 IST)
    # NSE Cash/FO: 09:15 to 15:30 IST (Upstox RMS auto-squareoff at 15:25 for Non-CAS & F&O, 15:10 for CAS; algo exit at 15:20)
    nse_open_minutes: int = 9 * 60 + 15
    nse_close_minutes: int = 15 * 60 + 30
    nse_mis_cutoff_minutes: int = 15 * 60 + 20
    # MCX Commodities: 09:00 to 23:30 IST (Upstox RMS auto-squareoff at 22:50 IST; algo exit at 22:45)
    mcx_open_minutes: int = 9 * 60 + 0
    mcx_close_minutes: int = 23 * 60 + 30
    mcx_mis_cutoff_minutes: int = 22 * 60 + 45
    # High-Momentum Session Windows (e.g. US Session 18:30 to 22:00 IST)
    us_session_start_minutes: int = 18 * 60 + 30
    us_session_end_minutes: int = 22 * 60 + 0


@dataclass
class OptionsConfig:
    risk_free_rate_pct: float = 7.0
    default_strike_offset: int = 0         # 0=ATM, 1=OTM1, -1=ITM1
    preferred_expiry: str = "CURRENT_WEEK" # CURRENT_WEEK, NEXT_WEEK, MONTHLY
    min_days_to_expiry: int = 0
    max_days_to_expiry: int = 30
    min_open_interest: int = 500
    target_delta_long: float = 0.50
    target_delta_short: float = 0.30
    max_iv_percentile: float = 85.0
    min_iv_percentile: float = 15.0


@dataclass
class InstrumentSpec:
    symbol: str
    asset_class: str
    exchange: str
    lot_size: int
    contract_multiplier: float
    tick_size: float
    strike_interval: float = 50.0
    margin_per_lot: Optional[float] = None


@dataclass
class FrameworkConfig:
    costs: StatutoryCostConfig = field(default_factory=StatutoryCostConfig)
    risk: RiskBudgetConfig = field(default_factory=RiskBudgetConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    custom_instruments: dict[str, InstrumentSpec] = field(default_factory=dict)

    @classmethod
    def load_from_file(cls, path: Path | str) -> FrameworkConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            costs=StatutoryCostConfig(**data.get("costs", {})),
            risk=RiskBudgetConfig(**data.get("risk", {})),
            session=SessionConfig(**data.get("session", {})),
            options=OptionsConfig(**data.get("options", {})),
        )

    def save_to_file(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "costs": self.costs.__dict__,
            "risk": self.risk.__dict__,
            "session": self.session.__dict__,
            "options": self.options.__dict__,
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


GLOBAL_CONFIG = FrameworkConfig()
