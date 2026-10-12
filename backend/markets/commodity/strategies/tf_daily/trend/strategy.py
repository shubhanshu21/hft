"""MCX commodity swing -- 50/200-day EMA trend, long and short. PAPER ONLY.

Rule (core/swing_engine "ema_trend", identical to what was backtested): long while EMA50 > EMA200 and the close is above EMA200,
short in the mirror case; exit when the EMAs cross back; initial stop 3 x ATR(14). Signals use completed daily bars of the
roll-honest ETF proxy (GLD / SLV / USO x USDINR, swing/proxy.ETF_PROXY) and are acted on at the next MCX morning, like the swing
strategy whose mechanics it reuses (stop on the real MCX price, gap-aware fill, contract roll 7 days before expiry).

Backtest (2026-10-12, markets/swing_research_ext on the ETF proxies, 5 commodities, costs 0.085% a round trip, no holding limit):
train 2005-18 105 trades +2.55% a trade PF 1.50; TEST 2019-26 94 trades +4.67% a trade PF 1.81, 6 of 8 years positive -- but t = 1.4
and random entries with the same exits averaged +1.13% a trade in 2019-26 (a commodity bull market): weak evidence, paper only.

One position per contract across strategies (as a real account nets them): it only trades a contract the 12-month swing is not
holding.
"""
from __future__ import annotations

import pandas as pd

from core.swing_engine import Rule
from markets.commodity.strategies.tf_daily.swing.proxy import etf_inr_frame, has_etf_proxy
from markets.commodity.strategies.tf_daily.swing.strategy import CommoditySwing


class CommodityTrend(CommoditySwing):
    name = "trend"
    id_prefix = "CTR"
    RULE = Rule("ema_trend", {"fast": 50, "slow": 200}, stop_mult=3.0)
    SETUP = "ema_50_200"

    def _frame(self, sym: str) -> pd.DataFrame:
        return etf_inr_frame(sym)

    def _has_proxy(self, sym: str) -> bool:
        return has_etf_proxy(sym)


STRATEGY = CommodityTrend()
