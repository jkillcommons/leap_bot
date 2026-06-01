"""
chain/diagonal_chain.py — Poor Man's Covered Call (PMCC / Diagonal Spread) selector.

Strategy:
  Long leg : deep ITM call, DTE ≥ 180, delta ≥ 0.75
  Short leg : OTM call, DTE 25-50, delta 0.20-0.35, strike > long strike
  Net debit must allow at least 1.5% monthly yield on capital at risk.

Key outputs:
  DiagonalCandidate — all metrics needed for display + paper trade logging.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────

DIAG_LONG_MIN_DTE    = 180
DIAG_LONG_MIN_DELTA  = 0.75

DIAG_SHORT_MIN_DTE   = 25
DIAG_SHORT_MAX_DTE   = 50
DIAG_SHORT_MIN_DELTA = 0.20
DIAG_SHORT_MAX_DELTA = 0.35

DIAG_MIN_CREDIT_PCT  = 0.015   # short credit must be ≥ 1.5% of net debit per month

Contract = dict  # type alias


@dataclass
class DiagonalCandidate:
    symbol: str
    stock_price: float

    # Long leg
    long_strike: float
    long_expiration: str
    long_dte: int
    long_delta: float
    long_mid: float
    long_bid: float
    long_ask: float

    # Short leg
    short_strike: float
    short_expiration: str
    short_dte: int
    short_delta: float
    short_mid: float
    short_bid: float
    short_ask: float

    # Combined metrics
    net_debit: float          # long_mid - short_mid
    max_profit: float         # (short_strike - long_strike) - net_debit  (if called away)
    max_loss: float           # net_debit (if both legs expire worthless)
    breakeven: float          # long_strike + net_debit
    monthly_yield_pct: float  # short_mid / net_debit  (as fraction, per ~30-day cycle)
    annual_yield_pct: float   # monthly_yield_pct * 12

    def display(self) -> str:
        lines = [
            f"  Symbol     : {self.symbol}  (${self.stock_price:.2f})",
            f"",
            f"  LONG  leg  : ${self.long_strike:.0f}C  {self.long_expiration}  "
            f"({self.long_dte} DTE)  δ={self.long_delta:.2f}  mid=${self.long_mid:.2f}",
            f"  SHORT leg  : ${self.short_strike:.0f}C  {self.short_expiration}  "
            f"({self.short_dte} DTE)  δ={self.short_delta:.2f}  mid=${self.short_mid:.2f}",
            f"",
            f"  Net debit  : ${self.net_debit:.2f}/share  (${self.net_debit*100:.0f}/contract)",
            f"  Short cred : ${self.short_mid:.2f}/share  (${self.short_mid*100:.0f}/contract)",
            f"  Monthly yld: {self.monthly_yield_pct*100:.1f}%",
            f"  Annual yld : {self.annual_yield_pct*100:.1f}%",
            f"  Breakeven  : ${self.breakeven:.2f}  at long expiration",
            f"  Max profit : ${self.max_profit:.2f}/share  (if called away at ${self.short_strike:.0f})",
            f"  Max loss   : ${self.max_loss:.2f}/share  (full debit if both legs worthless)",
        ]
        return "\n".join(lines)


class DiagonalChain:
    """Selects the best PMCC diagonal spread candidate from live option chains."""

    def __init__(self, broker, config=None):
        self._broker = broker
        self._config = config

    def select(self, symbol: str, stock_price: float) -> Optional[DiagonalCandidate]:
        """
        Fetch long and short chains, score all valid combinations,
        return the best DiagonalCandidate or None.
        """
        try:
            long_chain = self._broker.get_option_chain(
                symbol, "call",
                min_dte=DIAG_LONG_MIN_DTE,
                max_dte=730,
                underlying_price=stock_price,
            )
        except Exception as e:
            logger.warning("[%s] diagonal: long chain fetch failed: %s", symbol, e)
            return None

        try:
            short_chain = self._broker.get_option_chain(
                symbol, "call",
                min_dte=DIAG_SHORT_MIN_DTE,
                max_dte=DIAG_SHORT_MAX_DTE,
                underlying_price=stock_price,
            )
        except Exception as e:
            logger.warning("[%s] diagonal: short chain fetch failed: %s", symbol, e)
            return None

        if not long_chain or not short_chain:
            return None

        long_dicts  = [c.to_dict() if hasattr(c, "to_dict") else c for c in long_chain]
        short_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in short_chain]

        # Filter long legs
        long_legs = [
            c for c in long_dicts
            if _dte(c) >= DIAG_LONG_MIN_DTE
            and _delta(c) >= DIAG_LONG_MIN_DELTA
            and _mid(c) > 0
        ]
        if not long_legs:
            logger.info("[%s] diagonal: no long legs pass δ≥%.2f DTE≥%d",
                        symbol, DIAG_LONG_MIN_DELTA, DIAG_LONG_MIN_DTE)
            return None

        # Filter short legs
        short_legs = [
            c for c in short_dicts
            if DIAG_SHORT_MIN_DTE <= _dte(c) <= DIAG_SHORT_MAX_DTE
            and DIAG_SHORT_MIN_DELTA <= _delta(c) <= DIAG_SHORT_MAX_DELTA
            and _mid(c) > 0
        ]
        if not short_legs:
            logger.info("[%s] diagonal: no short legs pass δ %.2f-%.2f DTE %d-%d",
                        symbol, DIAG_SHORT_MIN_DELTA, DIAG_SHORT_MAX_DELTA,
                        DIAG_SHORT_MIN_DTE, DIAG_SHORT_MAX_DTE)
            return None

        # Pick best long leg: deepest delta (most intrinsic), then farthest DTE for time
        best_long = max(long_legs, key=lambda c: (_delta(c), _dte(c)))

        # Short leg must have strike > long strike
        long_strike = float(best_long.get("strike", 0))
        eligible_short = [
            c for c in short_legs
            if float(c.get("strike", 0)) > long_strike
        ]
        if not eligible_short:
            logger.info("[%s] diagonal: no short leg with strike > %.0f", symbol, long_strike)
            return None

        # Pick short leg: maximize monthly yield (short_mid / net_debit)
        best_cand = None
        best_yield = 0.0

        for sl in eligible_short:
            long_mid  = _mid(best_long)
            short_mid = _mid(sl)
            net_debit = long_mid - short_mid
            if net_debit <= 0:
                continue
            short_dte = _dte(sl)
            months = max(short_dte / 30.0, 0.1)
            monthly_yield = short_mid / net_debit / months  # per month
            if monthly_yield < DIAG_MIN_CREDIT_PCT:
                continue
            if monthly_yield > best_yield:
                best_yield = monthly_yield
                best_cand = sl

        if best_cand is None:
            logger.info("[%s] diagonal: no combination meets %.1f%% monthly yield",
                        symbol, DIAG_MIN_CREDIT_PCT * 100)
            return None

        long_mid   = _mid(best_long)
        short_mid  = _mid(best_cand)
        net_debit  = long_mid - short_mid
        short_stk  = float(best_cand.get("strike", 0))
        months     = max(_dte(best_cand) / 30.0, 0.1)
        monthly_y  = short_mid / net_debit / months if net_debit > 0 else 0

        return DiagonalCandidate(
            symbol           = symbol,
            stock_price      = stock_price,
            long_strike      = long_strike,
            long_expiration  = _exp(best_long),
            long_dte         = _dte(best_long),
            long_delta       = _delta(best_long),
            long_mid         = long_mid,
            long_bid         = float(best_long.get("bid", 0) or 0),
            long_ask         = float(best_long.get("ask", 0) or 0),
            short_strike     = short_stk,
            short_expiration = _exp(best_cand),
            short_dte        = _dte(best_cand),
            short_delta      = _delta(best_cand),
            short_mid        = short_mid,
            short_bid        = float(best_cand.get("bid", 0) or 0),
            short_ask        = float(best_cand.get("ask", 0) or 0),
            net_debit        = net_debit,
            max_profit       = max((short_stk - long_strike) - net_debit, 0),
            max_loss         = net_debit,
            breakeven        = long_strike + net_debit,
            monthly_yield_pct = monthly_y,
            annual_yield_pct  = monthly_y * 12,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mid(c: Contract) -> float:
    mid = c.get("mid") or c.get("mid_price")
    if mid:
        return float(mid)
    bid = float(c.get("bid") or 0)
    ask = float(c.get("ask") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return 0.0


def _delta(c: Contract) -> float:
    d = c.get("delta") or c.get("greeks", {}).get("delta") if isinstance(c.get("greeks"), dict) else c.get("delta")
    if d is None:
        return 0.0
    return abs(float(d))


def _dte(c: Contract) -> int:
    exp = c.get("expiration_date") or c.get("expiration")
    if not exp:
        return 0
    try:
        exp_date = date.fromisoformat(str(exp)[:10])
        return max((exp_date - date.today()).days, 0)
    except Exception:
        return 0


def _exp(c: Contract) -> str:
    exp = c.get("expiration_date") or c.get("expiration") or ""
    return str(exp)[:10]
