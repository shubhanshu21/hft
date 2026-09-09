"""
engine/backtester.py — Unified Multi-Asset Walk-Forward Backtesting Engine.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Any
import numpy as np
import pandas as pd

from framework.types import AssetClass, Direction, ExitReason
from framework.models import Position, Trade, CostBreakdown
from framework.strategy import BaseStrategy
from framework.config import FrameworkConfig, GLOBAL_CONFIG
from framework.costs import compute_statutory_costs


class MultiAssetBacktester:
    """
    Universal Walk-Forward Backtester for Equities, Futures, and Options.
    Evaluates strategies bar-by-bar with dynamic sizing, itemized statutory taxation, and equity tracking.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 100_000.0,
        config: Optional[FrameworkConfig] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.config = config or strategy.config or GLOBAL_CONFIG
        self.from_date = from_date
        self.to_date = to_date
        self.trades: list[Trade] = []
        self.equity_curve: list[float] = [initial_capital]

    def run(self, data_by_symbol: dict[str, pd.DataFrame], contract_multipliers: Optional[dict[str, float]] = None) -> dict[str, Any]:
        """
        Runs backtest across loaded symbol DataFrames.
        """
        multipliers = contract_multipliers or {}
        self.strategy.on_start(self.initial_capital)
        all_raw_trades: list[dict] = []

        for sym, raw_df in data_by_symbol.items():
            if raw_df is None or raw_df.empty:
                continue

            # Compute features
            feat_df = self.strategy.compute_features(raw_df, symbol=sym)
            n = len(feat_df)
            if n < 30:
                continue

            mult = multipliers.get(sym, 1.0)
            in_pos = False
            open_pos: Optional[Position] = None
            last_trade_day: Optional[str] = None

            if "timestamp" in feat_df.columns:
                timestamps = feat_df["timestamp"].values
            elif "date" in feat_df.columns:
                timestamps = feat_df["date"].values
            else:
                timestamps = feat_df.index.values

            for i in range(25, n):
                row = feat_df.iloc[i]
                c_time_str = str(timestamps[i])
                c_day = c_time_str[:10]

                # Date Filtering
                if self.from_date and c_day < self.from_date:
                    continue
                if self.to_date and c_day > self.to_date:
                    continue

                c_dt = pd.to_datetime(c_time_str)

                # 1. Manage Open Position
                if in_pos and open_pos:
                    exit_price, exit_reason = self.strategy.manage_position(
                        position=open_pos,
                        latest_bar=row,
                        now=c_dt,
                        bar_idx=i,
                    )

                    if exit_price is not None and exit_reason is not None:
                        # Cost & PnL calculation
                        costs = self.strategy.calculate_trade_costs(
                            direction=open_pos.direction,
                            entry_price=open_pos.entry_price,
                            exit_price=exit_price,
                            qty=open_pos.qty,
                            multiplier=mult,
                        )

                        self.capital += costs.net_pnl
                        self.equity_curve.append(self.capital)

                        trade = Trade(
                            trade_id=f"TRD_{len(self.trades)+1}",
                            position_id=open_pos.position_id,
                            symbol=sym,
                            asset_class=self.strategy.asset_class,
                            direction=open_pos.direction,
                            qty=open_pos.qty,
                            entry_price=open_pos.entry_price,
                            exit_price=exit_price,
                            entry_time=open_pos.entry_time,
                            exit_time=c_dt,
                            hold_minutes=(i - open_pos.entry_bar_idx) * 5.0,
                            exit_reason=exit_reason,
                            costs=costs,
                            capital_after=self.capital,
                        )
                        self.trades.append(trade)
                        all_raw_trades.append({
                            "symbol": sym,
                            "direction": open_pos.direction.value if hasattr(open_pos.direction, "value") else str(open_pos.direction),
                            "entry_time": str(open_pos.entry_time),
                            "exit_time": str(c_dt),
                            "entry_price": open_pos.entry_price,
                            "exit_price": exit_price,
                            "qty": open_pos.qty,
                            "gross_pnl": costs.gross_pnl,
                            "total_fees": costs.total_costs,
                            "net_pnl": costs.net_pnl,
                            "reason": exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason),
                            **costs.to_dict(),
                        })

                        in_pos = False
                        open_pos = None
                    continue

                # 2. Daily Entry Circuit Breaker (1 trade/day per symbol rule)
                if c_day == last_trade_day:
                    continue

                # 3. Generate Signals
                sig = self.strategy.generate_signal(
                    symbol=sym,
                    feat_df=feat_df,
                    current_bar=row,
                    bar_idx=i,
                )

                if sig is not None:
                    # Position sizing
                    qty = self.strategy.calculate_order_size(
                        symbol=sym,
                        capital=self.capital,
                        entry_price=sig["entry_price"],
                        stop_dist=sig["stop_distance"],
                        lot_size=sig.get("lot_size", 1),
                        multiplier=mult,
                    )

                    if qty > 0:
                        in_pos = True
                        last_trade_day = c_day
                        open_pos = Position(
                            position_id=f"POS_{len(self.trades)+1}_{sym}",
                            symbol=sym,
                            instrument_key=sym,
                            asset_class=self.strategy.asset_class,
                            direction=sig["direction"],
                            qty=qty,
                            entry_price=sig["entry_price"],
                            current_stop=sig["stop_loss"],
                            target_price=sig["take_profit"],
                            breakeven_price=sig["breakeven"],
                            entry_time=c_dt,
                            multiplier=mult,
                            best_price=sig["entry_price"],
                            armed_be=False,
                            entry_bar_idx=i,
                            p_score=sig.get("score", 0.5),
                            metadata=sig.get("metadata", {}),
                        )

        # Performance summary metrics
        total_trades = len(self.trades)
        wins = [t for t in self.trades if t.costs.net_pnl > 0]
        losses = [t for t in self.trades if t.costs.net_pnl <= 0]
        win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

        total_net_pnl = self.capital - self.initial_capital
        gross_profit = sum(t.costs.net_pnl for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.costs.net_pnl for t in losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        # Drawdown calculation
        eq_arr = np.array(self.equity_curve)
        peaks = np.maximum.accumulate(eq_arr)
        dds = (peaks - eq_arr) / peaks * 100.0
        max_dd = float(np.max(dds)) if len(dds) > 0 else 0.0

        return {
            "initial_capital": self.initial_capital,
            "final_capital": round(self.capital, 2),
            "total_net_pnl": round(total_net_pnl, 2),
            "roi_pct": round((total_net_pnl / self.initial_capital) * 100.0, 2),
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "trades": all_raw_trades,
        }
