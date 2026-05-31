"""
chain/put_chain.py — Deep ITM put strike selection for bearish directional trades.

Mirror of leap_chain.py but for puts.  Key differences:

  option_type  : "put" (not "call")
  Delta sign   : negative — target -0.70, range [-0.80, -0.60]
  DTE window   : 45–180 days (shorter than LEAPs; puts decay faster)
  Intrinsic    : max(K − S, 0) for puts (stock must be below strike to be ITM)
  Extrinsic    : mid − intrinsic_put
  Exit rules   : 75% gain target, 40% stop (tighter than calls)

Do NOT import from leap_chain.py — these are separate strategy modules.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, stdev
from typing import List, Optional

logger = logging.getLogger(__name__)

_RISK_FREE_RATE: float = 0.045

# ── Strategy parameters ───────────────────────────────────────────────────────

PUT_TARGET_DELTA   = -0.70   # ideal delta (negative for puts)
PUT_MIN_DELTA      = -0.80   # hard floor (more ITM = more negative)
PUT_MAX_DELTA      = -0.60   # hard ceiling (less ITM = less negative)
PUT_EXP_MIN_DAYS   = 45      # minimum DTE — avoid fast theta decay
PUT_EXP_MAX_DAYS   = 180     # maximum DTE — 6 months max
PUT_MIN_COST       = 2.00    # minimum mid price per share ($200/contract)
PUT_MIN_OI         = 50      # minimum open interest
PUT_MAX_SPREAD_PCT = 0.10    # max bid/ask as fraction of mid
PUT_TARGET_GAIN    = 0.75    # 75% gain target (exit sooner than calls)
PUT_MAX_LOSS       = 0.40    # stop at 40% loss (tighter than calls)
PUT_MAX_EXTRINSIC  = 0.40    # puts carry more extrinsic than deep ITM calls

Contract = dict  # type alias


# ── PutCandidate dataclass ────────────────────────────────────────────────────

@dataclass
class PutCandidate:
    """Structured result from select_put_strike()."""
    option_symbol:  str
    underlying:     str
    strike:         float
    expiration:     date
    delta:          Optional[float]
    mid_price:      float
    dte:            int
    iv:             Optional[float]
    bid:            Optional[float]
    ask:            Optional[float]
    open_interest:  int
    delta_source:   str = "live"   # "live", "bs", "none"
    price_source:   str = "live"   # "live", "bs", "none"

    # Derived at selection time
    intrinsic:      float = 0.0
    extrinsic:      float = 0.0
    breakeven:      float = 0.0    # strike − mid_price (stock must close below this)
    target_exit:    float = 0.0    # mid_price * (1 + PUT_TARGET_GAIN)
    stop_loss:      float = 0.0    # mid_price * (1 − PUT_MAX_LOSS)


# ── Math helpers ──────────────────────────────────────────────────────────────

def compute_historical_vol(closes: List[float], trading_days: int = 252) -> float:
    """Annualised realised vol from daily closes. Falls back to 0.25 on < 10 bars."""
    if len(closes) < 10:
        return 0.25
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    return stdev(log_returns) * math.sqrt(trading_days) if log_returns else 0.25


def _bs_d1_d2(S: float, K: float, T: float, sigma: float, r: float):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def _bs_put_delta(S: float, K: float, T: float, sigma: float,
                  r: float = _RISK_FREE_RATE) -> Optional[float]:
    """Black-Scholes delta for a European put. Returns a negative value."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1, _ = _bs_d1_d2(S, K, T, sigma, r)
        return round(NormalDist().cdf(d1) - 1.0, 4)
    except Exception:
        return None


def _bs_put_price(S: float, K: float, T: float, sigma: float,
                  r: float = _RISK_FREE_RATE) -> Optional[float]:
    """Black-Scholes theoretical price for a European put (per share)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        nd = NormalDist()
        d1, d2 = _bs_d1_d2(S, K, T, sigma, r)
        price = K * math.exp(-r * T) * nd.cdf(-d2) - S * nd.cdf(-d1)
        return max(round(price, 4), 0.01)
    except Exception:
        return None


def intrinsic_value_put(strike: float, underlying_price: float) -> float:
    """Intrinsic value of a put = max(K − S, 0)."""
    return max(strike - underlying_price, 0.0)


def extrinsic_value_put(mid: float, strike: float, underlying_price: float) -> float:
    """Extrinsic (time) value for a put = mid − intrinsic."""
    return max(mid - intrinsic_value_put(strike, underlying_price), 0.0)


def put_dte(expiration: date) -> int:
    """Days to expiration from today."""
    return (expiration - date.today()).days


def put_breakeven(strike: float, mid_price: float) -> float:
    """
    Put breakeven at expiration = strike − mid_price.
    Stock must close below this for the position to profit.
    """
    return strike - mid_price


def put_unrealized_pnl(entry_cost: float, current_mid: float,
                       contracts: int = 1) -> float:
    """P&L on an open long put (per-share cost × 100 × contracts)."""
    return (current_mid - entry_cost) * 100 * contracts


def should_exit_put_for_profit(
    entry_cost: float,
    current_mid: float,
    target_gain_pct: float = PUT_TARGET_GAIN,
) -> bool:
    """Return True if gain >= target_gain_pct of entry cost."""
    if entry_cost <= 0:
        return False
    return (current_mid - entry_cost) / entry_cost >= target_gain_pct


def should_exit_put_for_loss(
    entry_cost: float,
    current_mid: float,
    max_loss_pct: float = PUT_MAX_LOSS,
) -> bool:
    """Return True if loss >= max_loss_pct of entry cost."""
    if entry_cost <= 0:
        return False
    return (entry_cost - current_mid) / entry_cost >= max_loss_pct


# ── Filters ───────────────────────────────────────────────────────────────────

def _spread_ok(c: Contract, max_spread_pct: float) -> bool:
    bid, ask, mid = c.get("bid"), c.get("ask"), c.get("mid")
    if bid is None or ask is None or not mid:
        return True
    return (ask - bid) / mid <= max_spread_pct


def _extrinsic_ok_put(c: Contract, underlying_price: float,
                      max_extrinsic_pct: float = PUT_MAX_EXTRINSIC) -> bool:
    """Reject put if extrinsic > max_extrinsic_pct of mid."""
    mid = c.get("mid")
    if mid is None or not underlying_price:
        return True
    ext = extrinsic_value_put(mid, c.get("strike", 0), underlying_price)
    return (ext / mid) <= max_extrinsic_pct


def _put_rejection_reasons(
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
        # Put delta is negative: -0.80 <= delta <= -0.60
        if d < min_delta:
            reasons.append(
                f"delta too negative (δ={d:.3f} < floor {min_delta:.2f}) — too deep ITM"
            )
        if d > max_delta:
            reasons.append(
                f"delta not negative enough (δ={d:.3f} > ceiling {max_delta:.2f}) — not ITM enough"
            )

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
        ext = extrinsic_value_put(mid, c.get("strike", 0), underlying_price)
        ext_pct = ext / mid if mid else 0
        if ext_pct > max_extrinsic_pct:
            reasons.append(
                f"too much extrinsic (${ext:.2f} = {ext_pct:.0%} of mid, max {max_extrinsic_pct:.0%})"
            )

    return reasons


def _near_miss_report_puts(
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
    puts = [c for c in chain if (c.get("type") or c.get("option_type")) == "put"]
    if not puts:
        print("  (no put contracts in chain to diagnose)")
        return

    scored = []
    for c in puts:
        d     = c.get("delta")
        # Distance from target_delta (both negative)
        ddist = abs(d - target_delta) if d is not None else 999.0
        mid   = c.get("mid") or 0.0
        reasons = _put_rejection_reasons(
            c, target_delta, min_delta, max_delta,
            underlying_price, min_cost, min_open_interest,
            max_spread_pct, max_extrinsic_pct,
        )
        scored.append((ddist, -mid, c, reasons))

    scored.sort(key=lambda x: (x[0], x[1]))
    top = scored[:top_n]

    _BAR = "─" * 72
    print()
    print(f"  PUT NEAR-MISS DIAGNOSTICS — {len(scored)} put(s) rejected, showing top {len(top)}")
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
            ext = extrinsic_value_put(mid, strike, underlying_price)
            ext_s = f"${ext:.2f} ({ext / mid:.0%})"
        itm_s = f"${strike - underlying_price:.2f}" if underlying_price else "N/A"

        print(f"  #{rank}  {c.get('symbol',''):<26}  ${strike:>7.2f}P  {exp_s}  {dte_d:>4} DTE")
        print(f"       δ={delta_s}  mid={mid_s}  spread={spread_s}  "
              f"extrinsic={ext_s}  ITM by={itm_s}")
        for r in reasons:
            print(f"       ✗ {r}")
        if rank < len(top):
            print()

    print(f"  {_BAR}")


# ── Main selector ─────────────────────────────────────────────────────────────

def select_put_strike(
    chain: List[Contract],
    target_delta: float = PUT_TARGET_DELTA,
    min_delta: float = PUT_MIN_DELTA,
    max_delta: float = PUT_MAX_DELTA,
    underlying_price: float = 0.0,
    min_cost: float = PUT_MIN_COST,
    min_open_interest: int = PUT_MIN_OI,
    max_spread_pct: float = PUT_MAX_SPREAD_PCT,
    max_extrinsic_pct: float = PUT_MAX_EXTRINSIC,
) -> Optional[Contract]:
    """
    Select the best deep ITM put from a chain.

    Filters
    -------
    - option_type == "put"
    - delta in [min_delta, max_delta]  — both negative, e.g. [-0.80, -0.60]
    - mid >= min_cost
    - open_interest >= min_open_interest
    - spread <= max_spread_pct of mid
    - extrinsic <= max_extrinsic_pct of mid

    Returns the contract with delta closest to target_delta, or None.
    """
    candidates = [
        c for c in chain
        if (c.get("type") or c.get("option_type")) == "put"
        and c.get("delta") is not None
        and c.get("mid") is not None
        and c.get("mid", 0) >= min_cost
        and c.get("open_interest", 0) >= min_open_interest
        and min_delta <= c.get("delta") <= max_delta
        and _spread_ok(c, max_spread_pct)
        and _extrinsic_ok_put(c, underlying_price, max_extrinsic_pct)
    ]

    if not candidates:
        logger.warning(
            "No put candidates after filtering "
            "(δ=[%.2f,%.2f], min_cost=$%.2f, min_oi=%d, max_spread=%.0f%%, max_ext=%.0f%%)",
            min_delta, max_delta, min_cost, min_open_interest,
            max_spread_pct * 100, max_extrinsic_pct * 100,
        )
        _near_miss_report_puts(
            chain, target_delta, min_delta, max_delta,
            underlying_price, min_cost, min_open_interest,
            max_spread_pct, max_extrinsic_pct,
        )
        return None

    # Closest delta to target (both negative; abs difference)
    best = min(candidates, key=lambda c: abs(c.get("delta", 0) - target_delta))

    mid = best.get("mid") or 0
    bid = best.get("bid") or 0
    ask = best.get("ask") or 0
    intr = intrinsic_value_put(best.get("strike", 0), underlying_price) if underlying_price else 0
    ext  = extrinsic_value_put(mid, best.get("strike", 0), underlying_price) if underlying_price else 0

    logger.info(
        "Selected put: %s  strike=%.2f  δ=%.3f  mid=$%.2f  "
        "intrinsic=$%.2f  extrinsic=$%.2f (%.0f%%)  spread=%.0f%%  exp=%s",
        best.get("symbol", ""),
        best.get("strike", 0),
        best.get("delta", 0),
        mid, intr, ext,
        (ext / mid * 100) if mid else 0,
        ((ask - bid) / mid * 100) if mid else 0,
        best.get("expiration_date"),
    )
    return best


def build_put_candidate(contract: Contract, underlying: str,
                        underlying_price: float = 0.0) -> PutCandidate:
    """
    Convert a raw contract dict (from get_option_chain) into a PutCandidate.
    Computes derived fields: intrinsic, extrinsic, breakeven, target_exit, stop_loss.
    """
    exp = contract.get("expiration_date")
    if isinstance(exp, str):
        exp = date.fromisoformat(exp)
    mid  = contract.get("mid") or 0.0
    intr = intrinsic_value_put(contract.get("strike", 0), underlying_price)
    ext  = extrinsic_value_put(mid, contract.get("strike", 0), underlying_price)

    return PutCandidate(
        option_symbol  = contract.get("symbol", ""),
        underlying     = underlying.upper(),
        strike         = contract.get("strike", 0.0),
        expiration     = exp,
        delta          = contract.get("delta"),
        mid_price      = mid,
        dte            = put_dte(exp) if exp else 0,
        iv             = contract.get("implied_volatility"),
        bid            = contract.get("bid"),
        ask            = contract.get("ask"),
        open_interest  = contract.get("open_interest") or 0,
        delta_source   = contract.get("_delta_source", "none"),
        price_source   = contract.get("_price_source", "none"),
        intrinsic      = round(intr, 2),
        extrinsic      = round(ext, 2),
        breakeven      = round(put_breakeven(contract.get("strike", 0), mid), 2),
        target_exit    = round(mid * (1 + PUT_TARGET_GAIN), 2),   # per-share
        stop_loss      = round(mid * (1 - PUT_MAX_LOSS), 2),      # per-share
    )
