"""Portfolio-level entry gates shared by the paper runner (engine/live_dryrun.py) and the real-order
runner (engine/live_trading.py).

These used to be hand-copied into each runner, and the copies drifted: the real-order runner had no
margin cap and no heat cap on equity entries. Keeping them in ONE place means every strategy on
either runner passes through the same checks, and a fix lands in both.

A runner using this mixin must provide: self.capital, self.positions (symbol -> position dict that
carries "margin_used"), self.max_portfolio_heat_pct and self._portfolio_heat_pct().
"""
from __future__ import annotations

import os

from core.registry import market_of


class RiskGates:
    def _init_margin_limits(self) -> None:
        # Margin is ONE shared pool at the broker, but each strategy sizes its lots against the whole
        # account (its sizing is margin-bound: one trade routinely uses 80-100% of capital as margin), so
        # without a limit several open positions could together commit far more margin than exists.
        # A NEW entry is therefore sized to the margin still available (see _sizing_capital), not rejected.
        # 100% = the whole account, so the FIRST position is sized exactly as validated; lower values
        # shrink every position, and the per-market cap stops one market crowding out the others.
        # (90% / 50% here once rejected virtually every entry -- do not set these below what one
        # margin-bound trade needs unless smaller positions are intended.)
        self.max_margin_utilization_pct = float(os.environ.get("MAX_MARGIN_UTILIZATION_PCT", "100.0"))
        default_market_cap = float(os.environ.get("MAX_MARKET_MARGIN_UTILIZATION_PCT", "100.0"))
        self.max_market_margin_utilization_pct: dict[str, float] = {
            "commodity": float(os.environ.get("COMMODITY_MAX_MARGIN_PCT", default_market_cap)),
            "currency": float(os.environ.get("CURRENCY_MAX_MARGIN_PCT", default_market_cap)),
            "equity": float(os.environ.get("EQUITY_MAX_MARGIN_PCT", default_market_cap)),
        }

    def _total_margin_used(self) -> float:
        """Margin already committed by every open position, across all markets."""
        return sum(pos.get("margin_used", 0.0) for pos in self.positions.values())

    def _market_margin_used(self, market: str) -> float:
        return sum(pos.get("margin_used", 0.0) for sym, pos in self.positions.items() if market_of(sym) == market)

    def _margin_room(self, market: str) -> float:
        """Margin (rupees) still available to a NEW position in `market`: the tighter of the account-wide
        and the per-market limit, less what open positions have already committed."""
        total_room = self.capital * self.max_margin_utilization_pct / 100 - self._total_margin_used()
        market_cap = self.max_market_margin_utilization_pct.get(market, self.max_margin_utilization_pct)
        market_room = self.capital * market_cap / 100 - self._market_margin_used(market)
        return max(0.0, min(total_room, market_room))

    def _sizing_capital(self, market: str) -> float:
        """The capital handed to a strategy for sizing a new entry: the whole account while margin is free,
        less once other positions hold margin -- so the entry is sized to fit rather than rejected. This
        mirrors the pooled equity backtest, which sizes every entry against available margin."""
        return min(self.capital, self._margin_room(market))

    def _entry_gate_rejection(self, sym: str, market: str, signal, leverage: float) -> str | None:
        """None if the entry may proceed, else the reason it must not (heat cap, or -- as a last-resort
        backstop, since entries are already sized to the available margin -- a margin cap, e.g. when even
        one lot does not fit). `signal` is a core.strategy.Signal."""
        if self.capital <= 0:
            return None
        new_risk_rupees = signal.stop_dist * signal.qty * signal.lot_size
        if (self._portfolio_heat_pct() + new_risk_rupees / self.capital * 100) > self.max_portfolio_heat_pct:
            return f"would push total portfolio risk past the {self.max_portfolio_heat_pct:.1f}% heat cap"
        new_margin = signal.qty * signal.lot_size * signal.entry_price / leverage
        if (self._total_margin_used() + new_margin) / self.capital * 100 > self.max_margin_utilization_pct:
            return f"would push total committed margin past the {self.max_margin_utilization_pct:.1f}% cap"
        market_cap = self.max_market_margin_utilization_pct.get(market, self.max_margin_utilization_pct)
        if (self._market_margin_used(market) + new_margin) / self.capital * 100 > market_cap:
            return f"would push {market}'s own committed margin past its {market_cap:.1f}% sub-cap"
        return None
