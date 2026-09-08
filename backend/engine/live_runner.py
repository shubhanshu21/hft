"""
engine/live_runner.py — Unified Multi-Asset Live Paper-Trading & Dryrun Engine.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional, Any
from zoneinfo import ZoneInfo
import pandas as pd

from framework.types import AssetClass, Direction, ExitReason
from framework.models import Position, Quote, OptionChainSnapshot
from framework.strategy import BaseStrategy
from framework.config import FrameworkConfig, GLOBAL_CONFIG
from framework.database import TradingDB
from broker.upstox_broker import UpstoxBroker
from broker.instruments import get_instrument_key

IST = ZoneInfo("Asia/Kolkata")


class MultiAssetLiveRunner:
    """
    Universal Live Paper-Trading & Real-Time Scanning Engine.
    Connects to live broker candles, generates strategy signals, and records virtual orders/positions in SQLite.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        broker: UpstoxBroker,
        db: TradingDB,
        capital: float = 100_000.0,
        account_id: str = "LIVE_DRYRUN_ACCOUNT",
        interval_seconds: int = 60,
        config: Optional[FrameworkConfig] = None,
    ):
        self.strategy = strategy
        self.broker = broker
        self.db = db
        self.capital = capital
        self.account_id = account_id
        self.interval = interval_seconds
        self.config = config or strategy.config or GLOBAL_CONFIG
        self.positions: dict[str, Position] = {}

        # Sync DB account
        self.db.init_account(
            account_id=self.account_id,
            capital=self.capital,
            leverage=self.config.risk.default_leverage,
            risk_pct=self.config.risk.risk_pct_per_trade,
        )
        acct = self.db.get_account(self.account_id)
        if acct:
            self.capital = acct["current_capital"]

    def scan_once(self) -> list[dict]:
        now = datetime.now(IST)
        signals = []

        for sym in self.strategy.symbols:
            ikey = get_instrument_key(sym)
            if not ikey:
                continue

            raw = self.broker.get_intraday_candles(ikey, unit="minutes", interval=5)
            if not raw or len(raw) < 25:
                continue

            candles = list(reversed(raw))
            df = pd.DataFrame(candles)
            feat_df = self.strategy.compute_features(df, symbol=sym)
            if feat_df is None or len(feat_df) < 25:
                continue

            t = len(feat_df) - 1
            row = feat_df.iloc[t]

            # 1. Check exit if position is open
            if sym in self.positions:
                pos = self.positions[sym]
                exit_price, exit_reason = self.strategy.manage_position(
                    position=pos,
                    latest_bar=row,
                    now=now,
                    bar_idx=t,
                )

                if exit_price is not None and exit_reason is not None:
                    # Calculate itemized costs
                    costs = self.strategy.calculate_trade_costs(
                        direction=pos.direction,
                        entry_price=pos.entry_price,
                        exit_price=exit_price,
                        qty=pos.qty,
                        multiplier=pos.multiplier,
                    )

                    self.capital += costs.net_pnl

                    # Record Virtual Exit in DB
                    ts_tag = now.strftime("%Y%m%d_%H%M%S")
                    exit_order_id = f"ORD_X_{ts_tag}_{sym}"
                    exit_side = "SELL" if pos.direction in (Direction.LONG, "long") else "BUY"

                    self.db.place_order(
                        order_id=exit_order_id,
                        symbol=sym,
                        direction=exit_side,
                        intent=exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason),
                        order_type="MARKET",
                        qty=pos.qty,
                        requested_price=exit_price,
                        fill_price=exit_price,
                        status="FILLED",
                        tag=f"DRYRUN_EXIT_{exit_reason}",
                        account_id=self.account_id,
                    )

                    self.db.close_position(
                        position_id=pos.position_id,
                        exit_price=exit_price,
                        exit_reason=exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason),
                        gross_pnl=costs.gross_pnl,
                        net_pnl=costs.net_pnl,
                        total_fees=costs.total_costs,
                    )

                    self.db.record_trade(
                        position_id=pos.position_id,
                        symbol=sym,
                        direction=pos.direction.value if hasattr(pos.direction, "value") else str(pos.direction),
                        qty=pos.qty,
                        entry_price=pos.entry_price,
                        exit_price=exit_price,
                        entry_dt=pos.entry_time.isoformat(),
                        exit_dt=now.isoformat(),
                        hold_minutes=(now - pos.entry_time).total_seconds() / 60.0,
                        exit_reason=exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason),
                        gross_pnl=costs.gross_pnl,
                        costs_dict=costs.to_dict(),
                        net_pnl=costs.net_pnl,
                        capital_after=self.capital,
                        account_id=self.account_id,
                    )

                    self.db.record_snapshot(self.account_id)
                    del self.positions[sym]
                continue

            # 2. Evaluate entry signal
            sig = self.strategy.generate_signal(
                symbol=sym,
                feat_df=feat_df,
                current_bar=row,
                bar_idx=t,
            )

            if sig is not None:
                qty = self.strategy.calculate_order_size(
                    symbol=sym,
                    capital=self.capital,
                    entry_price=sig["entry_price"],
                    stop_dist=sig["stop_distance"],
                    lot_size=sig.get("lot_size", 1),
                )

                if qty > 0:
                    ts_tag = now.strftime("%Y%m%d_%H%M%S")
                    pos_id = f"POS_{ts_tag}_{sym}"
                    entry_order_id = f"ORD_E_{ts_tag}_{sym}"
                    order_side = "BUY" if sig["direction"] in (Direction.LONG, "long") else "SELL"

                    self.db.place_order(
                        order_id=entry_order_id,
                        symbol=sym,
                        direction=order_side,
                        intent="ENTRY",
                        order_type="MARKET",
                        qty=qty,
                        requested_price=sig["entry_price"],
                        fill_price=sig["entry_price"],
                        status="FILLED",
                        tag="DRYRUN_ENTRY",
                        account_id=self.account_id,
                    )

                    self.db.open_position(
                        position_id=pos_id,
                        symbol=sym,
                        direction=sig["direction"].value if hasattr(sig["direction"], "value") else str(sig["direction"]),
                        qty=qty,
                        entry_price=sig["entry_price"],
                        current_stop=sig["stop_loss"],
                        target_price=sig["take_profit"],
                        breakeven_price=sig["breakeven"],
                        account_id=self.account_id,
                    )

                    pos = Position(
                        position_id=pos_id,
                        symbol=sym,
                        instrument_key=ikey,
                        asset_class=self.strategy.asset_class,
                        direction=sig["direction"],
                        qty=qty,
                        entry_price=sig["entry_price"],
                        current_stop=sig["stop_loss"],
                        target_price=sig["take_profit"],
                        breakeven_price=sig["breakeven"],
                        entry_time=now,
                        entry_bar_idx=t,
                    )
                    self.positions[sym] = pos
                    signals.append(sig)

        return signals
