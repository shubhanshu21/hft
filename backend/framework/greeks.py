"""
framework/greeks.py — Black-Scholes Option Pricing, Implied Volatility Solver & Greeks Engine.
"""
from __future__ import annotations

import math
from typing import Optional
import numpy as np
from scipy.stats import norm

from .types import OptionType
from .models import Greeks


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float = 0.07,
    option_type: OptionType = OptionType.CALL,
) -> float:
    """
    Computes European Black-Scholes option price.
    """
    if time_to_expiry_years <= 0 or spot <= 0 or strike <= 0 or volatility <= 0:
        if option_type == OptionType.CALL or option_type == "CE":
            return max(0.0, spot - strike)
        else:
            return max(0.0, strike - spot)

    S = spot
    K = strike
    T = max(1e-5, time_to_expiry_years)
    v = max(1e-4, volatility)
    r = risk_free_rate

    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)

    if option_type == OptionType.CALL or option_type == "CE":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return max(0.0, float(price))


def compute_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float = 0.07,
    option_type: OptionType = OptionType.CALL,
) -> Greeks:
    """
    Computes Delta, Gamma, Theta (per day), Vega (per 1% IV change), and Rho.
    """
    if time_to_expiry_years <= 0 or spot <= 0 or strike <= 0 or volatility <= 0:
        d = 1.0 if (option_type in (OptionType.CALL, "CE") and spot >= strike) else 0.0
        return Greeks(iv=volatility, delta=d, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    S = spot
    K = strike
    T = max(1e-5, time_to_expiry_years)
    v = max(1e-4, volatility)
    r = risk_free_rate

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * sqrt_T)
    d2 = d1 - v * sqrt_T

    pdf_d1 = norm.pdf(d1)

    # Gamma (identical for Call & Put)
    gamma = pdf_d1 / (S * v * sqrt_T)

    # Vega (per 1% change in IV)
    vega = (S * pdf_d1 * sqrt_T) / 100.0

    if option_type in (OptionType.CALL, "CE"):
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * v) / (2.0 * sqrt_T) - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365.0
        rho = (K * T * math.exp(-r * T) * norm.cdf(d2)) / 100.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * v) / (2.0 * sqrt_T) + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365.0
        rho = (-K * T * math.exp(-r * T) * norm.cdf(-d2)) / 100.0

    return Greeks(
        iv=round(volatility, 4),
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega, 4),
        rho=round(rho, 4),
    )


def solve_implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float = 0.07,
    option_type: OptionType = OptionType.CALL,
    max_iterations: int = 50,
    tolerance: float = 1e-4,
) -> float:
    """
    Solves for Implied Volatility (IV) using Newton-Raphson method with Brent/Bisection fallback.
    """
    intrinsic = max(0.0, spot - strike if option_type in (OptionType.CALL, "CE") else strike - spot)
    if market_price <= intrinsic or time_to_expiry_years <= 0:
        return 0.20  # Default 20% IV fallback

    # Initial guess using Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2.0 * math.pi / time_to_expiry_years) * (market_price / spot)
    sigma = max(0.05, min(3.0, sigma))

    # Newton-Raphson iteration
    for _ in range(max_iterations):
        p = black_scholes_price(spot, strike, time_to_expiry_years, sigma, risk_free_rate, option_type)
        diff = p - market_price
        if abs(diff) < tolerance:
            return round(sigma, 4)

        # Vega calculation
        d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_to_expiry_years) / (sigma * math.sqrt(time_to_expiry_years))
        vega = spot * math.sqrt(time_to_expiry_years) * norm.pdf(d1)
        if vega < 1e-6:
            break

        sigma -= diff / vega
        if sigma <= 0.01 or sigma > 5.0:
            break

    # Bisection fallback
    low, high = 0.01, 4.0
    for _ in range(30):
        mid = (low + high) / 2.0
        p = black_scholes_price(spot, strike, time_to_expiry_years, mid, risk_free_rate, option_type)
        diff = p - market_price
        if abs(diff) < tolerance:
            return round(mid, 4)
        if diff > 0:
            high = mid
        else:
            low = mid

    return round((low + high) / 2.0, 4)
