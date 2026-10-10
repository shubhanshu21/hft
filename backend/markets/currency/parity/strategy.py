"""EURINR / GBPINR fair-value gap, 30-minute hold (signal.py has the research and the numbers). Paper trading only.

Fills are taken at the live best bid / ask (a long buys at the ask, a short sells at the bid, and the reverse on exit), so the paper
P&L already pays the real spread at that moment; costs() therefore adds only brokerage, exchange, SEBI, stamp duty and GST.

Switch on:  CURRENCY_STRATEGIES=scalping,parity  CURRENCY_PARITY_SYMBOLS=EURINR GBPINR  CURRENCY_SCALPING_SYMBOLS=USDINR
and EURINR GBPINR in DRYRUN_SYMBOLS. Lots: CURRENCY_PARITY_LOTS (default 5; brokerage is a fixed Rs70.8 a round trip, so 5 lots
cost ~1.3 bp of it against ~3 bp at 2 lots), capped by risk and the margin still free."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from core import sessions
from core.exits import open_after_entry, post_entry_range, stop_fill
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.currency.costs import compute_ncd_currency_costs, get_contract_multiplier, size_currency_lots
from markets.currency.parity.signal import (BAR, FX, HOLD_MIN, MAX_SPREAD_BP, STOP_BP, THRESHOLD_BP, closes, gap_bp, yahoo_fx_5m)
from services.utils.logger import get_logger

log = get_logger("parity")
HALF_SPREAD = {"EURINR": 0.0215, "GBPINR": 0.0200}       # Roll estimate / 2, used only if no live quote is available at exit
FIRST_ENTRY_MIN = 9 * 60 + 20                             # the 09:15 bar has completed


def book_fill(levels: list[tuple[float, int]] | None, lots: int) -> tuple[float | None, int]:
    """(average price, lots filled) of a market order for `lots` walking the book's levels (qty in lots). The crosses' best level
    often shows only 1-3 lots, so filling 5 lots at the touch would flatter the paper P&L."""
    if not levels:
        return None, 0
    need, cost, got = lots, 0.0, 0
    for px, qty in levels:
        take = min(need - got, max(int(qty), 0))
        cost += take * px
        got += take
        if got >= need:
            break
    return (cost / got, got) if got else (None, 0)


class CurrencyParity(Strategy):
    name, market = "parity", "currency"
    intraday = True
    product = "I"
    timeframe = ("minutes", 5)
    max_positions = 2
    id_prefix = "PAR"
    price_decimals = 4

    def __init__(self):
        self._last_bar: dict[str, object] = {}

    def lot_size(self, sym: str) -> int:
        return get_contract_multiplier(sym)

    def trades(self, symbol: str) -> bool:
        return symbol.upper() in FX and super().trades(symbol)

    def _last_entry_min(self) -> int:
        h, m = sessions.squareoff_clock(self.market)
        return h * 60 + m - HOLD_MIN - 5                   # the 30-minute hold ends before Upstox's square-off

    def entry(self, ctx: EntryContext) -> Signal | None:
        sym = ctx.symbol.upper()
        if sym not in FX or ctx.data is None:
            return None
        mins = ctx.now.hour * 60 + ctx.now.minute
        if not FIRST_ENTRY_MIN <= mins <= self._last_entry_min():
            return None
        own = closes(ctx.candles, ctx.now)
        if own.empty:
            return None
        bar = own.index[-1]
        if self._last_bar.get(sym) == bar:
            return None                                    # one decision per completed bar
        self._last_bar[sym] = bar
        if ctx.now - (bar + BAR).to_pydatetime() > timedelta(minutes=3):
            return None                                    # the bar closed too long ago (restart, feed lag): its signal is stale
        gap = gap_bp(own, closes(ctx.data.candles("USDINR"), ctx.now), yahoo_fx_5m(FX[sym])).get(bar)
        if gap is None or gap != gap or abs(gap) <= THRESHOLD_BP[sym]:
            return None
        direction = "short" if gap > 0 else "long"
        q = ctx.data.quote(sym)
        if not q:
            log.info("%s: gap %+.1f bp but no live quote -- skipped", sym, gap)
            return None
        spread_bp = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 1e4
        if spread_bp > MAX_SPREAD_BP:
            log.info("%s: gap %+.1f bp but the spread is %.1f bp -- skipped", sym, gap, spread_bp)
            return None
        d = 1 if direction == "long" else -1
        touch = q["ask"] if d == 1 else q["bid"]
        stop0 = touch * (1 - d * STOP_BP / 1e4)
        want = min(int(os.environ.get("CURRENCY_PARITY_LOTS", "5")),
                   size_currency_lots(ctx.capital, touch, abs(touch - stop0), ctx.risk_pct, sym, ctx.leverage))
        levels = q.get("asks" if d == 1 else "bids") or [(touch, want)]
        entry, lots = book_fill(levels, want)            # what a market order for `want` lots really pays; fewer lots if the book is thin
        if entry is None or lots < 1:
            return None
        entry = round(entry, 4)
        stop = round(entry * (1 - d * STOP_BP / 1e4), 4)
        log.info("%s: gap %+.1f bp -> %s %d lot(s) at %.4f (bid %.4f / ask %.4f, spread %.1f bp, %d of %d lots available in 5 levels)",
                 sym, gap, direction, lots, entry, q["bid"], q["ask"], spread_bp, lots, want)
        return Signal(symbol=sym, direction=direction, entry_price=entry, stop_loss=stop, qty=lots, stop_dist=abs(entry - stop),
                      instrument_key=ctx.instrument_key, lot_size=self.lot_size(sym), setup_type="parity_gap",
                      exit_state={"exit_after": (ctx.now + timedelta(minutes=HOLD_MIN)).isoformat(), "gap_bp": round(float(gap), 2),
                                  "spread_bp": round(spread_bp, 2)})

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        sym = ctx.symbol.upper()
        d = 1 if pos["direction"] == "long" else -1
        stop = float(pos["current_stop"])
        entry_bar = str(pos.get("entry_bar_ts", ""))
        for c in ctx.candles:
            if str(c["timestamp"]) < entry_bar:
                continue
            _, adv = post_entry_range(pos, c)
            if adv is not None and (adv <= stop if d == 1 else adv >= stop):
                return ExitDecision(round(stop_fill(stop, open_after_entry(pos, c), d), 4), "initial_stop")
        h, m = sessions.squareoff_clock(self.market)
        squareoff = ctx.now.replace(hour=h, minute=m, second=0, microsecond=0)
        exit_after = datetime.fromisoformat(str(pos["exit_after"])) if pos.get("exit_after") else squareoff
        if ctx.now < exit_after and ctx.now < squareoff:
            return None
        q = ctx.data.quote(sym) if ctx.data is not None else None
        lots = int(pos.get("lots", pos.get("qty", 1)) or 1)
        price = None
        if q:
            px, got = book_fill(q.get("bids" if d == 1 else "asks") or [(q["bid"] if d == 1 else q["ask"], lots)], lots)
            if px is not None and got >= lots:
                price = px
            elif px is not None:                            # the book shows fewer lots than we hold: the rest one tick beyond its last level
                last = (q.get("bids" if d == 1 else "asks") or [(px, 0)])[-1][0]
                price = (px * got + (last - d * 0.0025) * (lots - got)) / lots
        if price is None:                                   # no live book: the last trade, half a typical spread worse
            if not ctx.candles:
                return None
            price = float(ctx.candles[-1]["close"]) - d * HALF_SPREAD.get(sym, 0.02)
        return ExitDecision(round(price, 4), "time_exit" if ctx.now >= exit_after else "eod_squareoff")

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        """Fees only: the spread is already in the bid/ask fill prices."""
        c = compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)
        c["total"] = round(c["total"] - c["slippage"], 2)
        c["slippage"] = 0.0
        c["net"] = round(c["gross"] - c["total"], 2)
        return c


STRATEGY = CurrencyParity()
