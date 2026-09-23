"""
broker/base_broker.py — Abstract Broker Interface.

Defines the contract every broker implementation MUST fulfill.
The strategy layer only ever calls methods defined here, making
brokers fully interchangeable without touching strategy code.
"""

import time
from abc import ABC, abstractmethod
from datetime import datetime


class BaseBroker(ABC):
    """
    Abstract interface for all broker integrations.

    BaseBroker — the interface UpstoxBroker/PaperBroker/MockBroker implement:
      - get_ltp()             → fetch real-time spot price
      - get_option_contracts()→ list all available expiry dates
      - get_option_chain()    → fetch strikes + instrument tokens for an expiry
      - place_sell_order()    → place a SELL MARKET order for one options leg
    """

    @abstractmethod
    def get_ltp(self, instrument_key: str) -> float | None:
        """
        Fetch the Last Traded Price of an equity underlying.

        Args:
            instrument_key: Broker-specific identifier for the stock.

        Returns:
            LTP as float, or None on failure.
        """
        ...

    def get_ltp_batch(self, instrument_keys: list[str]) -> dict[str, float | None]:
        """
        Fetch LTPs for many instruments at once. Default implementation is
        just N calls to get_ltp() — correct for any broker, but callers that
        poll many symbols on a tight interval (e.g. api/market_broadcaster.py)
        should prefer a broker that overrides this with a real batched call
        (see UpstoxBroker.get_ltp_batch) to avoid N sequential round-trips
        and the per-symbol rate-limit flakiness that comes with them. Not
        abstract, so existing broker implementations don't need to change.
        """
        return {key: self.get_ltp(key) for key in instrument_keys}

    def get_market_depth(self, instrument_key: str) -> dict | None:
        """
        5-level bid/offer book + OHLC/volume/circuit-limit snapshot for one
        instrument. Not every broker implementation can provide this (paper
        trading has no real order book) — default is None, meaning "no depth
        data available," which callers must treat as a normal, expected
        outcome (show zeros), not an error. See UpstoxBroker.get_market_depth
        for the real implementation.
        """
        return None

    def get_ohlc_batch(self, instrument_keys: list[str]) -> dict[str, dict | None]:
        """
        {"ltp", "prev_close", "today_open", "today_high", "today_low"} for
        many instruments at once — default implementation is N calls to
        get_market_depth() (correct for any broker, but too slow/rate-limit-
        flaky for a large universe scanned all at once). Callers ranking
        many symbols against each other should prefer a broker that
        overrides this with a real batched call — see
        UpstoxBroker.get_ohlc_batch. Not abstract, so existing broker
        implementations don't need to change. Value is None for any
        instrument no depth data was available for.
        """
        results: dict[str, dict | None] = {}
        for key in instrument_keys:
            depth = self.get_market_depth(key)
            if depth is None:
                results[key] = None
                continue
            results[key] = {
                "ltp": depth["last_price"], "prev_close": depth["ohlc"]["close"],
                "today_open": depth["ohlc"]["open"], "today_high": depth["ohlc"]["high"], "today_low": depth["ohlc"]["low"],
            }
        return results

    def get_required_margin(
        self, instrument_key: str, quantity: int, transaction_type: str, product: str = "D",
    ) -> float | None:
        """
        Real broker-calculated SPAN + exposure margin (₹) required for ONE
        order — via Upstox's actual Margin Calculator API
        (ChargeApi.post_margin), not a flat-percentage guess. Default is
        None, meaning "no real margin figure available" — every caller
        (utils/margin.py::resolve_required_margin, the one place this
        should ever be called from) MUST fall back to the flat-rate
        estimate (estimate_margin_blocked) in that case, never treat None
        as "no margin needed."

        MockBroker (backtest) has no live broker to call and can't ask
        Upstox for a margin figure on a historical date — inherits this
        default (always None), so backtests keep using the documented
        flat-rate approximation, same as before. UpstoxBroker overrides
        this with the real API call; PaperBroker delegates to its wrapped
        real_broker so paper trading validates against the SAME real
        margin numbers a live order would.
        """
        return None

    def get_basket_required_margin(self, instruments: list[dict]) -> float | None:
        """
        Real broker-calculated NETTED margin for a whole basket of legs in
        ONE call — e.g. a short strangle's CE+PE together — via the same
        Upstox Margin Calculator API as get_required_margin(), but passing
        every instrument in a single request so the exchange's own
        hedge-benefit netting applies (a strangle needs meaningfully less
        combined margin than its two legs summed independently). Each
        dict: {instrument_key, quantity, transaction_type, product}.

        Default None (same unavailable-means-fall-back contract as
        get_required_margin) — MockBroker/backtest never overrides this.
        """
        return None

    def get_historical_candles(
        self, instrument_key: str, unit: str, interval: int, to_date: str, from_date: str | None = None,
    ) -> list[dict] | None:
        """
        Real historical OHLC candles for `instrument_key`, via the
        broker's own historical-data API — used by signal-driven custom
        strategies (e.g. gravity_strategy.py, macd_credit_strategy.py) to
        compute their indicators off REAL
        closed candles, never a locally-aggregated approximation from
        polled LTP ticks.

        Args:
            unit:      Broker-specific granularity unit, e.g. "minutes" or
                       "days" (UpstoxBroker's HistoryV3Api vocabulary).
            interval:  The multiplier for `unit` (e.g. 5 for 5-minute bars,
                       1 for daily bars).
            to_date:   'YYYY-MM-DD' — the most recent date to include; if
                       `from_date` is omitted, the broker decides how far
                       back the response goes.
            from_date: 'YYYY-MM-DD' — optional explicit start date, for
                       pulling a specific historical range (e.g. for
                       backtesting) rather than the broker's own default
                       lookback window.

        Returns a list of {"timestamp", "open", "high", "low", "close",
        "volume"} dicts, most-recent-first (caller's responsibility to
        reorder if it needs chronological order — see
        utils/technical_indicators.py, which expects oldest-first), or
        None if this broker doesn't support historical candles at all
        (MockBroker/backtest — a historical intraday strategy needs its
        own dedicated backtest data path, not this).
        """
        return None

    def get_available_funds(self) -> float | None:
        """
        Real available-to-trade balance (₹) on the actual broker account —
        Upstox's own "cash + pledge available to trade" figure, straight
        from their funds API, not a locally-simulated wallet. Default None
        ("not available from this broker") — MockBroker/backtest never
        overrides this, and PaperBroker deliberately does NOT delegate to
        its wrapped real_broker here: paper trading's capital pool is the
        per-user simulated wallet (utils/wallet.py::get_wallet_summary),
        a completely separate number from whatever is actually sitting in
        the one real Upstox account this app is configured against — only
        UpstoxBroker (the LIVE broker) overrides this.
        """
        return None

    @abstractmethod
    def get_option_contracts(self, instrument_key: str) -> list[str]:
        """
        Return all available option expiry dates for an underlying.

        Args:
            instrument_key: Broker-specific identifier.

        Returns:
            Sorted list of expiry strings ('YYYY-MM-DD').
        """
        ...

    @abstractmethod
    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        """
        Fetch full option chain data for an underlying + expiry.

        Args:
            instrument_key: Broker-specific identifier.
            expiry_date:    Expiry in 'YYYY-MM-DD' format.

        Returns:
            List of chain entries (structure varies per broker;
            consumed by broker-specific token finders).
        """
        ...

    @abstractmethod
    def refresh_instrument_master(self, force: bool = False) -> None:
        """
        Download (or reload from cache) today's instrument master.

        Must be called once before the trading session begins — typically
        at 09:00 IST via cron, before the market opens at 09:15 IST.

        Args:
            force: If True, re-download even if a fresh cache exists today.
        """
        ...

    @abstractmethod
    def resolve_instrument_key(self, symbol: str) -> str:
        """
        Dynamically resolve the broker-specific identifier for an NSE equity.

        Replaces all hardcoded symbol→token maps. Uses the daily instrument
        master to find the correct key at runtime.

        Args:
            symbol: NSE trading symbol (e.g., 'RELIANCE', 'TCS').

        Returns:
            Broker-specific instrument key/token/security_id string.

        Raises:
            RuntimeError: If the symbol is not found in the instrument master.
        """
        ...

    @abstractmethod
    def place_sell_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: int | None = None,
        is_close: bool = False,
    ) -> str | None:
        """
        Place a SELL order for an options leg.

        Args:
            instrument_token: Broker-specific token / trading symbol.
            quantity:         Total units (lot_size × lots).
            product:          'NRML' for overnight, 'MIS' for intraday.
            order_type:       'MARKET' or 'LIMIT'.
            tag:              Order tag / correlation ID.
            user_id:          Owning account — PaperBroker uses this to check
                               THAT account's own virtual wallet balance/margin
                               (each user has their own paper wallet, see
                               utils/wallet.py). Ignored by UpstoxBroker (a
                               live order checks the real broker's own real
                               margin, not our simulated one). Left None from
                               call sites with no real per-request user (the
                               legacy CLI daemon, backtest) — PaperBroker
                               skips its balance check in that case rather
                               than guessing whose wallet to charge.
            is_close:         True when this order is CLOSING/reducing an
                               existing position (exit, unwind, expiry
                               square-off) rather than opening new risk.
                               PaperBroker never rejects a close for
                               insufficient balance/margin — a real broker
                               always lets you reduce risk even at a loss;
                               only NEW exposure needs to clear a margin
                               check. Ignored by UpstoxBroker (the real
                               exchange enforces its own rules regardless).

        Returns:
            Order ID string on success, None on dry-run.
        """
        ...

    @abstractmethod
    def place_buy_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: int | None = None,
        is_close: bool = False,
    ) -> str | None:
        """
        Place a BUY order for an options leg.

        Used to square off (unwind) a leg that already sold successfully
        when a companion leg in the same basket fails to fill — see the
        calling strategy's partial-fill handling. Never used for opening a
        new long position; this codebase only ever sells to open.

        Args:
            instrument_token: Broker-specific token / trading symbol.
            quantity:         Total units (lot_size × lots).
            product:          'NRML' for overnight, 'MIS' for intraday.
            order_type:       'MARKET' or 'LIMIT'.
            tag:              Order tag / correlation ID.
            user_id:          Owning account — see place_sell_order's user_id.
            is_close:         See place_sell_order's is_close.

        Returns:
            Order ID string on success, None on dry-run.
        """
        ...

    def place_basket_sell_order(self, legs: list[dict]) -> dict:
        """
        Attempt to place several orders together, mixed BUY/SELL.

        Default implementation places them sequentially via
        `place_sell_order()`/`place_buy_order()`, one per leg (dispatched by
        each leg's own `transaction_type`). UpstoxBroker overrides this with
        a real single-call batch API (`POST /v2/order/multi/place`).

        NOT atomic on ANY broker, including Upstox — its own
        MultiOrderResponse.status can be "partial_success". Callers MUST
        handle partial fills (some legs filled, others None) themselves —
        see the calling strategy's auto-unwind logic.

        Args:
            legs: List of dicts, each with keys:
                correlation_id  — caller-chosen label used to map results
                                  back to legs (e.g. "CE"/"PE").
                transaction_type — "SELL" (default, for backward
                                  compatibility with pure-SELL baskets) or
                                  "BUY".
                instrument_token, quantity, product, order_type, tag,
                user_id, is_close
                                — same meaning as place_sell_order()'s /
                                  place_buy_order()'s args.

        Returns:
            Dict of correlation_id -> order_id (None if that leg failed or
            the broker is in dry-run mode).
        """
        results: dict = {}
        for leg in legs:
            place = self.place_buy_order if leg.get("transaction_type", "SELL") == "BUY" else self.place_sell_order
            results[leg["correlation_id"]] = place(
                instrument_token=leg["instrument_token"],
                quantity=leg["quantity"],
                product=leg.get("product", "NRML"),
                order_type=leg.get("order_type", "MARKET"),
                tag=leg.get("tag", ""),
                user_id=leg.get("user_id"),
                is_close=leg.get("is_close", False),
            )
        return results

    def get_current_time(self) -> datetime | None:
        """
        Return the timestamp the SEBI market-hours check should validate
        against, or None to use the real wall-clock time.

        Live and paper brokers use the default (None → real time). Simulated
        brokers (e.g. MockBroker in backtesting) override this to return the
        backtest's simulated current time, so the market-hours/weekend/holiday
        gate is evaluated against the historical date being simulated instead
        of whenever the backtest happens to actually run.
        """
        return None

    def get_lot_size(self, symbol: str) -> int | None:
        """
        Return the REAL current F&O lot size for `symbol`, resolved from
        this broker's live instrument master, or None if this broker
        doesn't support dynamic resolution.

        Default (None) means "not implemented for this broker" — callers
        must refuse to trade rather than guess/default to 1 (there is no
        hardcoded fallback table anywhere in this codebase; see
        TenPercentOTMStrangle.__init__()). MockBroker and UpstoxBroker
        override this today (MockBroker delegates to the same cached
        Upstox instrument master); NSE revises lot sizes periodically, so
        dynamic resolution from a daily-refreshed source is strictly more
        trustworthy than any static table would be.
        """
        return None

    def get_strike_step(self, symbol: str) -> float | None:
        """
        Return the REAL current strike-price interval for `symbol`,
        resolved from this broker's live instrument master (the actual
        gap between listed option strikes for the nearest expiry), or
        None if this broker doesn't support dynamic resolution.

        Same reasoning and same "no hardcoded fallback table, refuse to
        trade rather than guess" contract as get_lot_size() — verified
        live that a hardcoded strike-step table drifts out of date the
        same way a hardcoded lot-size table did (e.g. RELIANCE configured
        as 20, really 10; TCS configured as 50, really 20). Can be
        fractional for lower-priced stocks (e.g. 2.5), hence float not int.
        """
        return None

    def get_order_status(self, order_id: str) -> str | None:
        """
        Return the current status of a previously-placed order (e.g.
        'complete', 'rejected', 'cancelled', 'open'), lowercased, or None
        if this broker doesn't support status lookup or the order isn't found.

        Used for post-order reconciliation: an order API call can return
        an order_id and still have the exchange reject it afterward (e.g.
        insufficient margin) — a returned order_id alone does NOT guarantee
        the leg actually filled. Only UpstoxBroker overrides this today.
        """
        return None

    def get_fill_price(self, order_id: str) -> float | None:
        """
        Return the ACTUAL average price this order filled at, or None if
        that isn't known (order not found, not yet filled, or this broker
        doesn't track it).

        Callers that record entry_price/exit_price MUST prefer this over a
        get_ltp() snapshot taken before/after placing the order — LTP can
        move between the snapshot and the actual fill (slippage, a fast
        market), so a pre-order LTP is not what the account actually paid
        or received. Every concrete broker overrides this: PaperBroker/
        MockBroker return the slippage-adjusted price they simulated the
        fill at; UpstoxBroker queries the real order details API for the
        exchange's own reported average_price. Default None here exists so
        callers always have an explicit "unknown — fall back to LTP"
        signal rather than silently getting 0.0 or a stale value.
        """
        return None


def get_fill_price_with_retry(broker: "BaseBroker", order_id: str, attempts: int = 3, delay_sec: float = 1.0) -> float | None:
    """
    Poll broker.get_fill_price() a bounded number of times before giving up.

    get_fill_price() returns None until the exchange has actually confirmed
    the fill (see UpstoxBroker.get_fill_price) — for a real market order,
    that confirmation is not guaranteed to have landed yet the instant after
    placement, so a single immediate call frequently returns None even
    though the order does fill moments later, forcing callers to fall back
    to a less accurate LTP snapshot. This costs MockBroker/PaperBroker
    nothing since they fill synchronously and return a price on the first
    attempt.

    Kept short and bounded (not open-ended polling) because callers run on
    a single-threaded/sync scheduler loop (see strategy_scheduler.py) — a
    long poll here blocks every other strategy's tick. Mirrors the existing
    2-attempt/1s-delay retry already used for margin lookups in
    custom_strategy_scheduler.py's _try_entry.
    """
    for attempt in range(1, attempts + 1):
        fill_price = broker.get_fill_price(order_id)
        if fill_price is not None:
            return fill_price
        if attempt < attempts:
            time.sleep(delay_sec)
    return None
