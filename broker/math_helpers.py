"""
broker/math_helpers.py — Black-Scholes and volatility helpers for leap_bot.

Shared Black-Scholes and volatility helpers used across chain modules
without importing any broker-specific code.
"""

from __future__ import annotations

import math
from statistics import NormalDist, stdev as _stdev
from typing import List, Optional


def _compute_hist_vol(closes: List[float], trading_days: int = 252) -> float:
    """Annualised realised vol from daily closes. Falls back to 0.25 on < 10 bars."""
    if len(closes) < 10:
        return 0.25
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    return _stdev(log_returns) * math.sqrt(trading_days) if log_returns else 0.25


def _bs_call_delta(S: float, K: float, T: float, sigma: float,
                   r: float = 0.045) -> Optional[float]:
    """Black-Scholes delta for a European call."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return round(NormalDist().cdf(d1), 4)
    except Exception:
        return None


def _bs_call_price(S: float, K: float, T: float, sigma: float,
                   r: float = 0.045) -> Optional[float]:
    """Black-Scholes price for a European call."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        nd = NormalDist()
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        val = S * nd.cdf(d1) - K * math.exp(-r * T) * nd.cdf(d2)
        return max(round(val, 4), 0.01)
    except Exception:
        return None
