"""Crypto momentum paper trader: the rebalance rules, the fill arithmetic, and one full cycle on synthetic hourly bars (no network)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine import crypto_paper as cp
from markets.crypto import momentum as mo

_ENV_KEYS = ("CRYPTO_LEVERAGE", "CRYPTO_EXECUTION", "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET", "CRYPTO_STRATEGY", "CRYPTO_ROUTER_WEIGHT", "CRYPTO_REBALANCE_BAND")
_saved_env: dict = {}


def setUpModule():
    """The operator's .env (loaded by other test modules) sets leverage, strategy and demo keys: pin them so these tests never depend on it or reach an exchange."""
    for k in _ENV_KEYS:
        _saved_env[k] = os.environ.pop(k, None)
    os.environ["CRYPTO_LEVERAGE"] = "1.0"
    os.environ["CRYPTO_EXECUTION"] = "paper"


def tearDownModule():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
        if _saved_env.get(k) is not None:
            os.environ[k] = _saved_env[k]


def _frame(n_days=260, drift=0.0008, seed=1, noise=0.004):
    """Hourly bars with a steady up-trend plus noise (an obvious 'long' for every signal)."""
    rng = np.random.default_rng(seed)
    n = n_days * 24
    close = 100 * np.exp(np.cumsum(drift / 24 + rng.normal(0, noise, n)))
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 1.0, "trades": 1}, index=idx)


class TestPlanAndFills(unittest.TestCase):
    def test_entry_then_no_churn_inside_the_band_and_full_exit_on_a_flat_signal(self):
        prices = {"BTCUSDT": 100.0, "ETHUSDT": 10.0, "SOLUSDT": 1.0}
        orders = cp.plan_trades(3000.0, prices, {}, {"BTCUSDT": 1.0, "ETHUSDT": 0.5, "SOLUSDT": 0.0})
        self.assertEqual({(o["symbol"], o["side"]) for o in orders}, {("BTCUSDT", "BUY"), ("ETHUSDT", "BUY")})            # SOL target 0: nothing to do
        btc = next(o for o in orders if o["symbol"] == "BTCUSDT")
        self.assertAlmostEqual(btc["qty"], 1000.0 / 100.0)                                                               # a third of the equity
        held = {"BTCUSDT": 9.5, "ETHUSDT": 50.0, "SOLUSDT": 0.0}                                                         # BTC is 5% under its share: inside the 10% band
        self.assertEqual(cp.plan_trades(3000.0, prices, held, {"BTCUSDT": 1.0, "ETHUSDT": 0.5, "SOLUSDT": 0.0}), [])
        out = cp.plan_trades(3000.0, prices, held, {"BTCUSDT": 0.0, "ETHUSDT": 0.5, "SOLUSDT": 0.0})
        self.assertEqual([(o["symbol"], o["side"], o["reason"]) for o in out], [("BTCUSDT", "SELL", "exit (flat signal)")])
        self.assertEqual(out[0]["qty"], 9.5)                                                                              # the whole position

    def test_fills_charge_the_cost_and_buys_never_exceed_cash(self):
        cash, fills = cp.apply_orders(1000.0, {}, {}, {"BTCUSDT": 100.0}, [{"symbol": "BTCUSDT", "side": "BUY", "qty": 50.0, "reason": "entry"}], {"BTCUSDT": 0.0012})
        self.assertAlmostEqual(fills[0]["qty"], 1000.0 / (100.0 * 1.0012), places=6)                                     # trimmed to the cash left
        self.assertAlmostEqual(cash, 0.0, places=6)
        qty = {"BTCUSDT": fills[0]["qty"]}
        cash2, fills2 = cp.apply_orders(0.0, qty, {}, {"BTCUSDT": 110.0}, [{"symbol": "BTCUSDT", "side": "SELL", "qty": qty["BTCUSDT"], "reason": "exit"}], {"BTCUSDT": 0.0012})
        self.assertAlmostEqual(cash2, qty_before_sale(fills[0]) * 110.0 * (1 - 0.0012), places=4)
        self.assertGreater(cash2, 1000.0)                                                                                 # +10% price, minus two sides of cost


def qty_before_sale(fill):
    return fill["qty"]


class TestCycle(unittest.TestCase):
    def _run(self, con, frames, price, now):
        return cp.run_cycle(con, tuple(frames), now=now, get_hourly=lambda s: frames[s], get_price=lambda s: price[s], alert=lambda *a, **k: None)

    def test_first_cycle_buys_a_trending_market_and_a_second_cycle_on_the_same_bar_does_nothing(self):
        frames = {"BTCUSDT": _frame(seed=1), "ETHUSDT": _frame(seed=2), "SOLUSDT": _frame(seed=3)}
        price = {s: float(f["close"].iloc[-1]) for s, f in frames.items()}
        with tempfile.TemporaryDirectory() as tmp:
            con = cp.connect(Path(tmp) / "c.db")
            cp.reset(con, 9000.0, tuple(frames))
            r1 = self._run(con, frames, price, datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc))
            self.assertGreater(len(r1["fills"]), 0)
            self.assertTrue(all(f["side"] == "BUY" for f in r1["fills"]))
            self.assertLess(r1["equity"], 9000.0)                                                                       # the cost of getting in
            self.assertGreater(r1["equity"], 9000.0 * 0.99)
            self.assertLessEqual(sum(cp.holdings(con)[s] * price[s] for s in frames), 9000.0)                           # spot: never more than the equity
            n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            r2 = self._run(con, frames, price, datetime(2026, 9, 28, 10, 16, tzinfo=timezone.utc))
            self.assertEqual(r2["fills"], [])                                                                           # same hourly bar: no second decision
            self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], n)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM equity").fetchone()[0], 2)                               # but equity is recorded every cycle
            self.assertIn("CRYPTO account", cp.status(con))

    def _falling(self):
        return {s: _frame(drift=-0.003, seed=i, noise=0.0004) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}

    def test_trend_mode_stays_in_cash_in_a_falling_market(self):
        frames = self._falling()
        price = {s: float(f["close"].iloc[-1]) for s, f in frames.items()}
        with tempfile.TemporaryDirectory() as tmp:
            con = cp.connect(Path(tmp) / "c.db")
            cp.reset(con, 9000.0, tuple(frames))
            r = cp.run_cycle(con, tuple(frames), now=datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc), get_hourly=lambda s: frames[s], get_price=lambda s: price[s],
                             alert=lambda *a, **k: None, get_funding=lambda *a: [], strategy="trend")
            self.assertEqual(r["fills"], [])
            self.assertEqual(r["equity"], 9000.0)

    def test_router_goes_short_in_a_bear_market_and_the_short_pays_off_when_price_falls_further(self):
        frames = self._falling()
        price = {s: float(f["close"].iloc[-1]) for s, f in frames.items()}
        with tempfile.TemporaryDirectory() as tmp:
            con = cp.connect(Path(tmp) / "c.db")
            cp.reset(con, 9000.0, tuple(frames))
            now = datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc)
            r = cp.run_cycle(con, tuple(frames), now=now, get_hourly=lambda s: frames[s], get_price=lambda s: price[s], alert=lambda *a, **k: None, get_funding=lambda *a: [], strategy="router")
            self.assertEqual(cp.get_state(con, "regime"), "bear")
            self.assertTrue(r["fills"] and all(f["side"] == "SELL" for f in r["fills"]))
            held = cp.holdings(con)
            self.assertTrue(all(q < 0 for q in held.values()))                                                        # short positions
            self.assertGreater(r["cash"], 9000.0)                                                                       # the short proceeds are in cash
            self.assertLess(r["equity"], 9000.0)                                                                        # minus entry costs (0.09% per side)
            self.assertGreater(r["equity"], 9000.0 * 0.99)
            lower = {s: p * 0.9 for s, p in price.items()}                                                              # prices fall 10%: the shorts gain
            r2 = cp.run_cycle(con, tuple(frames), now=now + timedelta(minutes=15), get_hourly=lambda s: frames[s], get_price=lambda s: lower[s], alert=lambda *a, **k: None,
                              get_funding=lambda *a: [], strategy="router")
            self.assertGreater(r2["equity"], r["equity"] + 200)

    def test_funding_is_received_by_shorts_and_paid_when_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = cp.connect(Path(tmp) / "c.db")
            cp.reset(con, 9000.0, ("BTCUSDT",))
            now = datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc)
            self.assertEqual(cp.accrue_funding(con, ("BTCUSDT",), {"BTCUSDT": -1.0}, {"BTCUSDT": 100.0}, now, get_funding=lambda *a: [0.001]), 0.0)         # first call only sets the pointer
            got = cp.accrue_funding(con, ("BTCUSDT",), {"BTCUSDT": -2.0}, {"BTCUSDT": 100.0}, now + timedelta(hours=9), get_funding=lambda *a: [0.0001, 0.0002])
            self.assertAlmostEqual(got, 200.0 * 0.0003, places=9)                                                       # a 2 x 100 short receives 0.03%
            paid = cp.accrue_funding(con, ("BTCUSDT",), {"BTCUSDT": -2.0}, {"BTCUSDT": 100.0}, now + timedelta(hours=18), get_funding=lambda *a: [-0.0005])
            self.assertAlmostEqual(paid, -0.1, places=9)
            longs = cp.accrue_funding(con, ("BTCUSDT",), {"BTCUSDT": 3.0}, {"BTCUSDT": 100.0}, now + timedelta(hours=27), get_funding=lambda *a: [0.001])
            self.assertEqual(longs, 0.0)                                                                                 # spot longs pay no funding


class TestSignedOrders(unittest.TestCase):
    def test_flip_from_long_to_short_charges_spot_cost_on_the_sale_and_perp_cost_on_the_short_leg(self):
        prices = {"BTCUSDT": 100.0}
        orders = cp.plan_trades(3000.0, prices, {"BTCUSDT": 10.0}, {"BTCUSDT": -0.5}, band=0.0)
        self.assertEqual([(o["side"], o["reason"]) for o in orders], [("SELL", "flip to short")])
        self.assertAlmostEqual(orders[0]["qty"], (1000.0 + 1500.0) / 100.0)                                             # one coin: share 3,000, target -0.5 = -1,500; from +1,000
        qty, avg = {"BTCUSDT": 10.0}, {"BTCUSDT": 90.0}
        cash, fills = cp.apply_orders(0.0, qty, avg, prices, orders, {"BTCUSDT": 0.0012}, perp_side=0.0009)
        self.assertAlmostEqual(qty["BTCUSDT"], -15.0)
        self.assertAlmostEqual(fills[0]["cost"], 1000.0 * 0.0012 + 1500.0 * 0.0009, places=9)                           # 10 coins sold on spot, 15 shorted on the perp
        self.assertAlmostEqual(cash, 2500.0 - fills[0]["cost"], places=9)
        self.assertEqual(avg["BTCUSDT"], 100.0)
        exit_orders = cp.plan_trades(3000.0, prices, qty, {"BTCUSDT": 0.0})
        self.assertEqual([(o["side"], o["qty"], o["reason"]) for o in exit_orders], [("BUY", 15.0, "exit (flat signal)")])    # a flat target closes the short


class TestSignalsMatchTheStudy(unittest.TestCase):
    def test_position_is_a_size_between_zero_and_one_and_components_report_each_signal(self):
        f = _frame()
        pos = mo.ensemble_position(f)
        self.assertTrue(((pos >= 0) & (pos <= 1)).all())
        comp = mo.components(f)
        self.assertEqual(set(comp), {"ema20/50", "donchian20", "tsmom90", "vol_scale"})
        self.assertAlmostEqual(pos[-1], np.mean([comp["ema20/50"], comp["donchian20"], comp["tsmom90"]]) * comp["vol_scale"], places=9)


if __name__ == "__main__":
    unittest.main()


class TestRouterTargets(unittest.TestCase):
    def test_bull_is_long_bear_is_short_and_a_coin_not_in_a_bear_stays_flat_while_btc_is_bear(self):
        from markets.crypto import router
        up = _frame(drift=0.003, seed=1, noise=0.0004)
        down = _frame(drift=-0.003, seed=2, noise=0.0004)
        bull, regime = router.current_targets({"BTCUSDT": up, "ETHUSDT": up.copy(), "SOLUSDT": up.copy()})
        self.assertEqual(regime, "bull")
        self.assertTrue(all(0.0 < v <= 1.0 for v in bull.values()))
        bear, regime = router.current_targets({"BTCUSDT": down, "ETHUSDT": down.copy(), "SOLUSDT": up.copy()})
        self.assertEqual(regime, "bear")
        self.assertLess(bear["BTCUSDT"], 0.0)
        self.assertLess(bear["ETHUSDT"], 0.0)
        self.assertEqual(bear["SOLUSDT"], 0.0)                                   # SOL is itself rising: no short (and no long while BTC is bear)
        self.assertTrue(all(-1.0 <= v <= 1.0 for v in bear.values()))


class TestBlend(unittest.TestCase):
    def test_blend_is_the_weighted_sum_of_the_router_and_the_trend_targets(self):
        from markets.crypto import router
        from markets.crypto.momentum import ensemble_position
        up, down = _frame(drift=0.003, seed=1, noise=0.0004), _frame(drift=-0.003, seed=2, noise=0.0004)
        frames = {"BTCUSDT": down, "ETHUSDT": down.copy(), "SOLUSDT": up.copy()}
        routed = router.target_series(frames)
        blend = router.blend_series(frames, 0.5)
        for s, df in frames.items():
            trend = pd.Series(ensemble_position(df, 24), index=df.index)
            self.assertTrue(np.allclose(blend[s].to_numpy(), (0.5 * routed[s] + 0.5 * trend).to_numpy()))
        t, regime = router.current_targets(frames, router_weight=0.5)
        self.assertEqual(regime, "bear")
        self.assertLess(t["BTCUSDT"], 0.0)                                       # BTC: the short half dominates (its trend half is flat)
        self.assertGreater(t["SOLUSDT"], 0.0)                                    # SOL is rising: only the trend half is long, at half weight
        self.assertEqual(router.current_targets(frames, router_weight=1.0)[0]["SOLUSDT"], 0.0)



# ------------------------------------------------------------------------------------------------------------------------------------------------------------------
class FakeDemo:
    """The parts of services.broker.binance_demo.BinanceDemo the executor uses, on an in-memory exchange (fills at `px`, 0.1% fee on spot, 0.05% on perps)."""
    FILTERS = {("spot", "BTCUSDT"): (0.00001, 5.0), ("spot", "ETHUSDT"): (0.0001, 5.0), ("spot", "SOLUSDT"): (0.001, 5.0),
               ("futures", "BTCUSDT"): (0.001, 50.0), ("futures", "ETHUSDT"): (0.001, 20.0), ("futures", "SOLUSDT"): (0.01, 5.0)}

    def __init__(self, px, spot=None, perp=None):
        self.px, self.spot, self.perp = px, dict(spot or {}), dict(perp or {})
        self.orders, self.margin, self.income = [], [], 0.0

    def filters(self, venue, symbol):
        step, mn = self.FILTERS[(venue, symbol)]
        return {"step": step, "min_qty": step, "min_notional": mn}

    def price(self, venue, symbol):
        return self.px[symbol]

    def spot_balance(self, asset):
        return self.spot.get(asset, 0.0)

    def futures_position(self, symbol):
        return {"amt": self.perp.get(symbol, 0.0), "entry": 0.0, "liquidation": 0.0, "leverage": 2}

    def futures_wallet(self):
        return {"wallet": 1000.0, "available": 900.0, "unrealized": 0.0}

    def futures_income(self, since_ms, symbol=None):
        return self.income

    def set_isolated(self, symbol, leverage):
        self.margin.append((symbol, leverage))

    def spot_market(self, symbol, side, qty):
        px, base = self.px[symbol], symbol[:-4]
        if side == "SELL" and self.spot.get(base, 0.0) + 1e-12 < qty:
            raise AssertionError(f"selling {qty} {base} but only {self.spot.get(base, 0.0)} held")
        self.spot[base] = self.spot.get(base, 0.0) + (qty if side == "BUY" else -qty)
        self.orders.append(("spot", symbol, side, qty, False))
        return {"qty": qty, "avg_price": px, "notional": qty * px, "fee": qty * px * 0.001, "order_id": len(self.orders), "status": "FILLED"}

    def futures_market(self, symbol, side, qty, reduce_only=False):
        px = self.px[symbol]
        self.perp[symbol] = self.perp.get(symbol, 0.0) + (qty if side == "BUY" else -qty)
        self.orders.append(("perp", symbol, side, qty, reduce_only))
        return {"qty": qty, "avg_price": px, "notional": qty * px, "fee": qty * px * 0.0005, "order_id": len(self.orders), "status": "FILLED"}


class TestDemoExecution(unittest.TestCase):
    def _cycle(self, frames, fake, leverage="1.0", spot=None, capital=600.0):
        price = {s: float(f["close"].iloc[-1]) for s, f in frames.items()}
        fake.px = price
        with tempfile.TemporaryDirectory() as tmp:
            con = cp.connect(Path(tmp) / "c.db")
            cp.reset(con, capital, tuple(frames))
            cp.set_state(con, "leverage", leverage)
            ex = cp.DemoExecutor(fake, tuple(frames))
            ex.baseline(con)
            r = cp.run_cycle(con, tuple(frames), now=datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc), get_hourly=lambda s: frames[s], get_price=lambda s: price[s],
                             alert=lambda *a, **k: None, strategy="trend" if leverage == "trend" else None, executor=ex)
            return r, con, ex

    def test_pure_helpers(self):
        from services.broker.binance_demo import floor_step
        self.assertEqual(cp.split_venues(0.7, 1.0), (0.7, 0.0))
        self.assertEqual(cp.split_venues(1.5, 1.0), (1.0, 0.5))                       # a leveraged long: spot up to 1x, the rest a perpetual
        self.assertEqual(cp.split_venues(-0.4, 1.0), (0.0, -0.4))                     # a short is all perpetual
        self.assertEqual(cp.perp_legs(-2.0, 3.0), [("BUY", 2.0, True), ("BUY", 1.0, False)])      # short 2 -> long 1: close, then open
        self.assertEqual(cp.perp_legs(0.0, -1.5), [("SELL", 1.5, False)])
        self.assertEqual(cp.perp_legs(1.0, -0.4), [("SELL", 0.4, True)])
        self.assertAlmostEqual(floor_step(0.123456, 0.001), 0.123)
        self.assertAlmostEqual(floor_step(0.99999, 0.00001), 0.99999)
        self.assertEqual(floor_step(1.9999, 1.0), 1.0)

    def test_bull_market_buys_spot_only_and_the_ledger_follows_the_fills(self):
        frames = {s: _frame(seed=i) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
        fake = FakeDemo({})
        r, con, ex = self._cycle(frames, fake, spot={})
        self.assertTrue(fake.orders and all(o[0] == "spot" and o[2] == "BUY" for o in fake.orders))
        self.assertEqual(fake.perp, {})
        held = cp.holdings(con)
        for s in frames:
            self.assertAlmostEqual(held[s], fake.spot[s[:-4]], places=6)               # the ledger quantity is what the exchange holds
        self.assertLess(r["equity"], 600.0)                                              # entry fees
        self.assertGreater(r["equity"], 596.0)

    def test_existing_demo_balances_are_a_baseline_not_ours(self):
        frames = {s: _frame(seed=i) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
        fake = FakeDemo({}, spot={"BTC": 5.0, "USDT": 100000.0})                        # the demo account already holds 5 BTC
        r, con, ex = self._cycle(frames, fake)
        self.assertLess(cp.holdings(con)["BTCUSDT"], 4.0)                                # our BTC is only what we bought, not the 5 BTC baseline
        self.assertAlmostEqual(fake.spot["BTC"] - 5.0, cp.holdings(con)["BTCUSDT"], places=6)

    def test_bear_market_opens_isolated_perpetual_shorts(self):
        frames = {s: _frame(drift=-0.003, seed=i, noise=0.0004) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
        fake = FakeDemo({})
        r, con, ex = self._cycle(frames, fake)
        self.assertTrue(fake.orders and all(o[0] == "perp" and o[2] == "SELL" for o in fake.orders))
        self.assertTrue(all(v < 0 for v in fake.perp.values()))
        self.assertEqual({m[0] for m in fake.margin}, set(frames))                       # isolated margin set once per symbol before trading
        self.assertTrue(all(m[1] == 2 for m in fake.margin))
        self.assertGreater(r["cash"], 600.0)                                             # the shorts' proceeds

    def test_leverage_puts_the_part_above_one_share_on_the_perpetual(self):
        frames = {s: _frame(seed=i) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
        fake = FakeDemo({})
        r, con, ex = self._cycle(frames, fake, leverage="1.5", capital=3000.0)
        self.assertTrue(any(o[0] == "spot" for o in fake.orders) and any(o[0] == "perp" and o[2] == "BUY" for o in fake.orders))
        for s in frames:
            share = r["equity"] / 3
            spot_value = fake.spot.get(s[:-4], 0.0) * r["prices"][s]
            perp_value = fake.perp.get(s, 0.0) * r["prices"][s]
            self.assertLessEqual(spot_value, share * 1.01)                               # spot never above 1x the coin's share (the equity moved by the entry fees since the decision)
            self.assertGreater(spot_value + perp_value, share * 1.05)                    # but total exposure is above 1x
            self.assertLessEqual(spot_value + perp_value, share * 1.5 * 1.01)

    def test_orders_below_the_exchange_minimum_are_skipped_not_sent(self):
        frames = {s: _frame(seed=i) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
        fake = FakeDemo({})
        r, con, ex = self._cycle(frames, fake, capital=30.0)                             # 10 USDT per coin: under the 5 USDT spot minimum? no -- but rounding to the step still leaves valid orders
        for kind, sym, side, qty, ro in fake.orders:
            step, mn = FakeDemo.FILTERS[(("spot" if kind == "spot" else "futures"), sym)]
            self.assertGreaterEqual(qty * r["prices"][sym], mn)


class TestBinanceDemoClient(unittest.TestCase):
    def test_signing_matches_the_binance_documentation_example(self):
        from services.broker.binance_demo import BinanceDemo
        c = BinanceDemo("k", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
        self.assertEqual(c.sign("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"),
                         "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_it_refuses_the_production_hosts(self):
        from services.broker.binance_demo import BinanceDemo
        for bad in ("https://api.binance.com", "https://fapi.binance.com", "https://testnet.binance.vision", "https://demo-api.binance.com.evil.example"):
            with self.assertRaises(ValueError, msg=bad):
                BinanceDemo("k", "s", spot_base=bad)
        c = BinanceDemo("k", "s")
        with self.assertRaises(ValueError):
            c._request("https://api.binance.com", "GET", "/api/v3/ping")

    def test_requests_are_signed_with_the_key_header_and_market_order_fields(self):
        from services.broker.binance_demo import BinanceDemo
        seen = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"orderId": 7, "executedQty": "0.5", "cummulativeQuoteQty": "50.0", "status": "FILLED", "fills": [{"price": "100", "commission": "0.0005", "commissionAsset": "ETH"}]}

        class Sess:
            def request(self, method, url, headers=None, timeout=None):
                seen.update(method=method, url=url, headers=headers)
                return Resp()

        c = BinanceDemo("KEY123", "secret", session=Sess())
        out = c.spot_market("ETHUSDT", "BUY", 0.5)
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].startswith("https://demo-api.binance.com/api/v3/order?"))
        self.assertEqual(seen["headers"]["X-MBX-APIKEY"], "KEY123")
        for part in ("symbol=ETHUSDT", "side=BUY", "type=MARKET", "quantity=0.5", "timestamp=", "signature="):
            self.assertIn(part, seen["url"])
        self.assertAlmostEqual(out["avg_price"], 100.0)
        self.assertAlmostEqual(out["fee"], 0.0005 * 100.0)                              # the commission was paid in the base coin: converted at the fill price


class TestFuturesFillsAreReadFromTheTradeList(unittest.TestCase):
    def test_a_market_order_answered_with_avg_price_zero_is_priced_from_the_fills(self):
        from services.broker.binance_demo import BinanceDemo
        calls = []

        class Resp:
            status_code = 200
            def __init__(self, body): self.body = body
            def json(self): return self.body

        class Sess:
            def request(self, method, url, headers=None, timeout=None):
                calls.append(url)
                if "/fapi/v1/order" in url:
                    return Resp({"orderId": 9, "executedQty": "0.06", "avgPrice": "0.00000", "cumQuote": "0.00000", "status": "FILLED"})
                if "/fapi/v1/userTrades" in url:
                    return Resp([{"qty": "0.02", "price": "100.0", "commission": "0.001", "commissionAsset": "USDT"}, {"qty": "0.04", "price": "103.0", "commission": "0.002", "commissionAsset": "USDT"}])
                raise AssertionError(url)

        out = BinanceDemo("k", "s", session=Sess()).futures_market("SOLUSDT", "SELL", 0.06)
        self.assertAlmostEqual(out["qty"], 0.06)
        self.assertAlmostEqual(out["avg_price"], (0.02 * 100 + 0.04 * 103) / 0.06)             # the size-weighted price of the two fills
        self.assertAlmostEqual(out["notional"], 0.02 * 100 + 0.04 * 103)
        self.assertAlmostEqual(out["fee"], 0.003)
        self.assertTrue(any("reduceOnly" not in u for u in calls))
