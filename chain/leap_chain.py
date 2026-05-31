"""
chain/leap_chain.py — LEAP strike selection helpers.

Mirrors wheel_bot's option_chain.py pattern but adapted for deep ITM
long calls with 12–24 month expirations.

Key differences
---------------
- Targeting delta 0.70–0.85 (deep ITM) vs wheel_bot's 0.15–0.25 (OTM puts)
- Cost floor in dollars per share ($5.00+) vs wheel's $0.30–$0.50
- Intrinsic value check: reject if extrinsic > MAX_EXTRINSIC_PCT of mid
- No cushion check (that's a CSP concept); replaced by intrinsic value quality check
- Spread filter uses absolute dollar threshold, not just pct, for expensive LEAPs
"""

from __future__ import annotations

import logging
import math
from datetime import date
from statistics import NormalDist, stdev
from typing import List, Optional

logger = logging.getLogger(__name__)

_RISK_FREE_RATE: float = 0.045

# Maximum extrinsic value as a fraction of mid price.
# LEAPs with > 30% extrinsic are buying too much time premium.
MAX_EXTRINSIC_PCT: float = 0.30

Contract = dict  # type alias


# ── Math helpers ──────────────────────────────────────────────────────────────

def compute_historical_vol(closes: List[float], trading_days: int = 252) -> float:
    """Annualised realised vol from daily closes. Falls back to 0.25 on < 10 bars."""
    if len(closes) < 10:
        return 0.25
    log_returns = [
        math.log(closes[i] / closes[i-1])
        for i in range(1, len(closes))
        if closes[i-1] > 0
    ]
    return stdev(log_returns) * math.sqrt(trading_days) if log_returns else 0.25


def _bs_d1_d2(S: float, K: float, T: float, sigma: float, r: float):
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def _bs_call_delta(S: float, K: float, T: float, sigma: float,
                   r: float = _RISK_FREE_RATE) -> Optional[float]:
    """Black-Scholes delta for a European call."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1, _ = _bs_d1_d2(S, K, T, sigma, r)
        return round(NormalDist().cdf(d1), 4)
    except Exception:
        return None


def _bs_call_price(S: float, K: float, T: float, sigma: float,
                   r: float = _RISK_FREE_RATE) -> Optional[float]:
    """Black-Scholes theoretical price for a European call."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        nd = NormalDist()
        d1, d2 = _bs_d1_d2(S, K, T, sigma, r)
        price = S * nd.cdf(d1) - K * math.exp(-r * T) * nd.cdf(d2)
        return max(round(price, 4), 0.01)
    except Exception:
        return None


def intrinsic_value(strike: float, underlying_price: float) -> float:
    """Intrinsic value of a call = max(S - K, 0)."""
    return max(underlying_price - strike, 0.0)


def extrinsic_value(mid: float, strike: float, underlying_price: float) -> float:
    """Extrinsic (time) value = mid - intrinsic."""
    return max(mid - intrinsic_value(strike, underlying_price), 0.0)


# ── Filters ───────────────────────────────────────────────────────────────────

def _spread_ok(c: Contract, max_spread_pct: float) -> bool:
    bid, ask, mid = c.get("bid"), c.get("ask"), c.get("mid")
    if bid is None or ask is None or not mid:
        return True
    return (ask - bid) / mid <= max_spread_pct


def _extrinsic_ok(c: Contract, underlying_price: float,
                  max_extrinsic_pct: float = MAX_EXTRINSIC_PCT) -> bool:
    """Reject LEAP if extrinsic value > max_extrinsic_pct of mid price."""
    mid = c.get("mid")
    if mid is None or not underlying_price:
        return True
    ext = extrinsic_value(mid, c.get("strike", 0), underlying_price)
    return (ext / mid) <= max_extrinsic_pct


def _rejection_reasons(
    c: Contract,
    target_delta: float,
    min_delta: float,
    max_delta: float,
    underlying_price: float,
    min_cost: float,
    min_open_interest: int,
    max_spread_pct: float,
    max_extrinsic_pct: float,
) -> List[str]:
    reasons: List[str] = []
    d   = c.get("delta")
    mid = c.get("mid")
    oi  = c.get("open_interest") or 0
    bid, ask = c.get("bid"), c.get("ask")

    if d is None:
        reasons.append("no delta (Greeks unavailable)")
    else:
        if d < min_delta:
            reasons.append(f"delta too low (δ={d:.3f} < min {min_delta:.2f}) — not deep enough ITM")
        if d > max_delta:
            reasons.append(f"delta too high (δ={d:.3f} > max {max_delta:.2f})")

    if mid is None:
        reasons.append("no mid price")
    elif mid < min_cost:
        reasons.append(f"cost too low (${mid:.2f} < ${min_cost:.2f} min)")

    if oi < min_open_interest:
        reasons.append(f"OI too low ({oi} < {min_open_interest} min)")

    if bid and ask and mid:
        spread_pct = (ask - bid) / mid
        if spread_pct > max_spread_pct:
            reasons.append(f"spread too wide ({spread_pct:.0%} > {max_spread_pct:.0%} max)")

    if mid and underlying_price:
        ext    = extrinsic_value(mid, c.get("strike", 0), underlying_price)
        ext_pct = ext / mid
        if ext_pct > max_extrinsic_pct:
            reasons.append(
                f"too much extrinsic (${ext:.2f} = {ext_pct:.0%} of mid, max {max_extrinsic_pct:.0%})"
            )

    return reasons


def _near_miss_report(
    chain: List[Contract],
    target_delta: float,
    min_delta: float,
    max_delta: float,
    underlying_price: float,
    min_cost: float,
    min_open_interest: int,
    max_spread_pct: float,
    max_extrinsic_pct: float,
    top_n: int = 5,
) -> None:
    today = date.today()
    calls = [c for c in chain if (c.get("type") or c.get("option_type")) == "call"]
    if not calls:
        print("  (no call contracts to diagnose)")
        return

    scored = []
    for c in calls:
        d     = c.get("delta")
        ddist = abs(d - target_delta) if d is not None else 999.0
        mid   = c.get("mid") or 0.0
        reasons = _rejection_reasons(
            c, target_delta, min_delta, max_delta,
            underlying_price, min_cost, min_open_interest,
            max_spread_pct, max_extrinsic_pct,
        )
        scored.append((ddist, -mid, c, reasons))

    scored.sort(key=lambda x: (x[0], x[1]))
    top = scored[:top_n]

    _BAR = "─" * 72
    print()
    print(f"  NEAR-MISS DIAGNOSTICS — {len(scored)} call(s) rejected, showing top {len(top)}")
    print(f"  δ=[{min_delta:.2f}–{max_delta:.2f}]  target δ={target_delta:.2f}  "
          f"min_cost=${min_cost:.2f}  min_OI={min_open_interest}  "
          f"max_spread={max_spread_pct:.0%}  max_extrinsic={max_extrinsic_pct:.0%}")
    print(f"  {_BAR}")

    for rank, (ddist, neg_mid, c, reasons) in enumerate(top, 1):
        strike  = c.get("strike") or 0.0
        exp     = c.get("expiration_date")
        exp_s   = exp.strftime("%b-%d-%Y") if exp else "N/A"
        dte_d   = (exp - today).days if exp else 0
        d       = c.get("delta")
        delta_s = f"{d:+.3f}" if d is not None else "  N/A"
        mid     = c.get("mid")
        mid_s   = f"${mid:.2f}" if mid is not None else "N/A"
        bid, ask = c.get("bid"), c.get("ask")
        spread_s = (
            f"{(ask - bid) / mid:.0%}" if bid and ask and mid else "N/A"
        )
        ext_s = "N/A"
        if mid and underlying_price:
            ext = extrinsic_value(mid, strike, underlying_price)
            ext_s = f"${ext:.2f} ({ext/mid:.0%})"

        print(f"  #{rank}  {c.get('symbol',''):<26}  ${strike:>7.2f}  {exp_s}  {dte_d:>4} DTE")
        print(f"       δ={delta_s}  mid={mid_s}  spread={spread_s}  extrinsic={ext_s}")
        for r in reasons:
            print(f"       ✗ {r}")
        if rank < len(top):
            print()

    print(f"  {_BAR}")


# ── Main selector ─────────────────────────────────────────────────────────────

def select_leap_call(
    chain: List[Contract],
    target_delta: float = 0.80,
    min_delta: float = 0.70,
    max_delta: float = 0.90,
    underlying_price: float = 0.0,
    min_cost: float = 5.00,
    min_open_interest: int = 50,
    max_spread_pct: float = 0.10,
    max_extrinsic_pct: float = MAX_EXTRINSIC_PCT,
) -> Optional[Contract]:
    """
    Select the best LEAP call from a chain.

    Filters
    -------
    - delta in [min_delta, max_delta]   — deep ITM target
    - mid >= min_cost                   — minimum dollar cost per share
    - open_interest >= min_open_interest
    - spread <= max_spread_pct of mid
    - extrinsic value <= max_extrinsic_pct of mid

    Returns the contract closest to target_delta, or None if none qualify.

    Parameters
    ----------
    chain            : List of OptionContract or dicts from get_option_chain().
    target_delta     : Ideal delta (default 0.80).
    min_delta        : Hard floor (default 0.70).
    max_delta        : Hard ceiling (default 0.90).
    underlying_price : Used for extrinsic value check.
    min_cost         : Minimum mid price per share (LEAPs are expensive).
    min_open_interest: Liquidity floor.
    max_spread_pct   : Spread / mid floor.
    max_extrinsic_pct: Reject if > this fraction of mid is extrinsic.
    """
    candidates = [
        c for c in chain
        if (c.get("type") or c.get("option_type")) == "call"
        and c.get("delta") is not None
        and c.get("mid") is not None
        and c.get("mid", 0) >= min_cost
        and c.get("open_interest", 0) >= min_open_interest
        and min_delta <= c.get("delta") <= max_delta
        and _spread_ok(c, max_spread_pct)
        and _extrinsic_ok(c, underlying_price, max_extrinsic_pct)
    ]

    if not candidates:
        logger.warning(
            "No LEAP candidates after filtering "
            "(δ=[%.2f,%.2f], min_cost=$%.2f, min_oi=%d, max_spread=%.0f%%, max_ext=%.0f%%)",
            min_delta, max_delta, min_cost, min_open_interest,
            max_spread_pct * 100, max_extrinsic_pct * 100,
        )
        _near_miss_report(
            chain, target_delta, min_delta, max_delta,
            underlying_price, min_cost, min_open_interest,
            max_spread_pct, max_extrinsic_pct,
        )
        return None

    best = min(candidates, key=lambda c: abs(c.get("delta") - target_delta))
    mid  = best.get("mid") or 0
    bid  = best.get("bid") or 0
    ask  = best.get("ask") or 0
    ext  = extrinsic_value(mid, best.get("strike", 0), underlying_price) if underlying_price else 0

    logger.info(
        "Selected LEAP: %s  strike=%.2f  δ=%.3f  mid=$%.2f  "
        "extrinsic=$%.2f (%.0f%%)  spread=%.0f%%  exp=%s",
        best.get("symbol", ""),
        best.get("strike", 0),
        best.get("delta", 0),
        mid,
        ext,
        (ext / mid * 100) if mid else 0,
        ((ask - bid) / mid * 100) if mid else 0,
        best.get("expiration_date"),
    )
    return best


def dte(expiration: date) -> int:
    """Days to expiration from today."""
    return (expiration - date.today()).days


def breakeven(strike: float, entry_cost: float) -> float:
    """
    LEAP breakeven at expiration = strike + entry cost per share.
    (Underlying must trade above this for the position to profit.)
    """
    return strike + entry_cost


def unrealized_pnl(entry_cost: float, current_mid: float, contracts: int = 1) -> float:
    """P&L on an open long call position (per-share costs × 100 × contracts)."""
    return (current_mid - entry_cost) * 100 * contracts


def should_exit_for_profit(
    entry_cost: float,
    current_mid: float,
    target_gain_pct: float = 1.00,  # 100% = double your money
) -> bool:
    """
    Return True if the position has gained >= target_gain_pct of entry cost.
    Default target is 100% gain (2x the entry price).
    """
    if entry_cost <= 0:
        return False
    return (current_mid - entry_cost) / entry_cost >= target_gain_pct


def should_exit_for_loss(
    entry_cost: float,
    current_mid: float,
    max_loss_pct: float = 0.50,  # cut at 50% loss
) -> bool:
    """Return True if the position has lost >= max_loss_pct of entry cost."""
    if entry_cost <= 0:
        return False
    return (entry_cost - current_mid) / entry_cost >= max_loss_pct
