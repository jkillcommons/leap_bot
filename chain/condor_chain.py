"""
chain/condor_chain.py — Iron Condor strike selector (Play 4).

Iron Condor = sell OTM put + sell OTM call + buy further OTM put + buy further OTM call.
Net credit strategy. Profits when underlying stays between the short strikes at expiration.

Requirements
------------
- VIX > 28 (enforced by caller / mode_condor)
- IV Rank > 40 preferred (enforced by caller)
- DTE: 21–45 days
- Short strikes: ~0.15–0.20 delta (OTM)
- Wing width: $10 for ETFs, $5 for stocks
- Min net credit: 25% of wing width
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CondorResult:
    symbol:            str
    expiration:        str
    short_put_strike:  float
    long_put_strike:   float
    short_call_strike: float
    long_call_strike:  float
    short_put_symbol:  str
    long_put_symbol:   str
    short_call_symbol: str
    long_call_symbol:  str
    net_credit:        float   # premium received per share (positive)
    max_loss:          float   # wing width - credit (per share)
    break_even_low:    float   # short put strike - net credit
    break_even_high:   float   # short call strike + net credit
    delta_put:         float   # abs delta of short put
    delta_call:        float   # abs delta of short call
    dte:               int
    iv_rank:           Optional[float] = None

    def display(self) -> str:
        lines = [
            f"Iron Condor: {self.symbol}  Exp: {self.expiration}  DTE: {self.dte}",
            f"  Short put:   ${self.short_put_strike:.1f}P  δ={self.delta_put:.2f}",
            f"  Long put:    ${self.long_put_strike:.1f}P",
            f"  Short call:  ${self.short_call_strike:.1f}C  δ={self.delta_call:.2f}",
            f"  Long call:   ${self.long_call_strike:.1f}C",
            f"  Net credit:  ${self.net_credit:.2f}/share  (${self.net_credit*100:.0f}/contract)",
            f"  Max loss:    ${self.max_loss:.2f}/share",
            f"  B/E range:   ${self.break_even_low:.2f} – ${self.break_even_high:.2f}",
        ]
        if self.iv_rank is not None:
            lines.append(f"  IV rank:     {self.iv_rank:.0f}")
        return "\n".join(lines)


class CondorChain:
    """
    Selects an Iron Condor setup for a symbol using live Tradier chain data.

    Caller is responsible for VIX and IV rank gates before calling select().
    """

    WING_WIDTH_ETF   = 10.0
    WING_WIDTH_STOCK =  5.0
    ETF_SYMBOLS      = {"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XLF", "XLE"}
    TARGET_DELTA     = 0.17    # ideal short strike delta
    MIN_CREDIT_PCT   = 0.25    # min credit as fraction of wing width
    MIN_DTE          = 21
    MAX_DTE          = 45

    def __init__(self, broker) -> None:
        self.broker = broker

    def select(self, symbol: str, spot: float) -> Optional[CondorResult]:
        """
        Fetch the option chain and return the best Iron Condor setup,
        or None if no valid setup exists.
        """
        wing = self.WING_WIDTH_ETF if symbol in self.ETF_SYMBOLS \
               else self.WING_WIDTH_STOCK

        # Get expiration dates in the 21–45 DTE window
        exp = self._pick_expiration(symbol)
        if not exp:
            logger.warning("[%s] CondorChain: no expiration in %d–%d DTE window",
                           symbol, self.MIN_DTE, self.MAX_DTE)
            return None

        # Fetch chain for short strike selection (spread-filtered for liquidity)
        # and again unfiltered for wing legs (far-OTM wings have naturally wide spreads)
        calls = self.broker.get_option_chain(
            symbol, "call", min_dte=self.MIN_DTE, max_dte=self.MAX_DTE
        )
        puts = self.broker.get_option_chain(
            symbol, "put", min_dte=self.MIN_DTE, max_dte=self.MAX_DTE
        )
        calls_wide = self.broker.get_option_chain(
            symbol, "call", min_dte=self.MIN_DTE, max_dte=self.MAX_DTE, max_spread_pct=1.0
        )
        puts_wide = self.broker.get_option_chain(
            symbol, "put", min_dte=self.MIN_DTE, max_dte=self.MAX_DTE, max_spread_pct=1.0
        )

        # Filter all chains to chosen expiration only
        calls      = [c for c in calls      if str(getattr(c, "expiration_date", "")).startswith(exp)]
        puts       = [c for c in puts       if str(getattr(c, "expiration_date", "")).startswith(exp)]
        calls_wide = [c for c in calls_wide if str(getattr(c, "expiration_date", "")).startswith(exp)]
        puts_wide  = [c for c in puts_wide  if str(getattr(c, "expiration_date", "")).startswith(exp)]

        if not calls or not puts:
            logger.warning("[%s] CondorChain: empty chain for %s", symbol, exp)
            return None

        # Convert OptionContract objects to dicts for uniform handling
        call_dicts      = [c.to_dict() if hasattr(c, "to_dict") else c for c in calls]
        put_dicts       = [c.to_dict() if hasattr(c, "to_dict") else c for c in puts]
        call_dicts_wide = [c.to_dict() if hasattr(c, "to_dict") else c for c in calls_wide]
        put_dicts_wide  = [c.to_dict() if hasattr(c, "to_dict") else c for c in puts_wide]

        # Short strikes from spread-filtered chain (ensures liquidity)
        short_put  = self._pick_short_strike(put_dicts,  spot, "put")
        short_call = self._pick_short_strike(call_dicts, spot, "call")

        if not short_put or not short_call:
            logger.warning("[%s] CondorChain: could not find short strikes near δ=%.2f",
                           symbol, self.TARGET_DELTA)
            return None

        long_put_strike  = round(short_put["strike"]  - wing, 1)
        long_call_strike = round(short_call["strike"] + wing, 1)

        # Wing strikes from unfiltered chain (far-OTM wings have naturally wide spreads)
        long_put  = self._find_strike(puts_wide,  long_put_strike)
        long_call = self._find_strike(calls_wide, long_call_strike)

        if not long_put or not long_call:
            logger.warning("[%s] CondorChain: wing strikes %.1f/%.1f not in chain",
                           symbol, long_put_strike, long_call_strike)
            return None

        net_credit = round(
            self._mid(short_put)  - self._mid(long_put) +
            self._mid(short_call) - self._mid(long_call),
            2,
        )

        min_credit = round(wing * self.MIN_CREDIT_PCT, 2)
        if net_credit < min_credit:
            logger.info("[%s] CondorChain: credit $%.2f below minimum $%.2f — skip",
                        symbol, net_credit, min_credit)
            return None

        dte_val = self._dte(exp)

        return CondorResult(
            symbol            = symbol,
            expiration        = exp,
            short_put_strike  = float(short_put["strike"]),
            long_put_strike   = long_put_strike,
            short_call_strike = float(short_call["strike"]),
            long_call_strike  = long_call_strike,
            short_put_symbol  = short_put.get("symbol", ""),
            long_put_symbol   = long_put.get("symbol", ""),
            short_call_symbol = short_call.get("symbol", ""),
            long_call_symbol  = long_call.get("symbol", ""),
            net_credit        = net_credit,
            max_loss          = round(wing - net_credit, 2),
            break_even_low    = round(float(short_put["strike"])  - net_credit, 2),
            break_even_high   = round(float(short_call["strike"]) + net_credit, 2),
            delta_put         = abs(float(short_put.get("delta") or 0)),
            delta_call        = abs(float(short_call.get("delta") or 0)),
            dte               = dte_val,
        )

    # ── private helpers ───────────────────────────────────────────────────────

    def _pick_expiration(self, symbol: str) -> Optional[str]:
        """Return earliest expiration inside the 21–45 DTE window."""
        try:
            exp_data = self.broker._get("/markets/options/expirations", {
                "symbol": symbol, "includeAllRoots": "true", "strikes": "false",
            })
            raw = (exp_data.get("expirations") or {}).get("date") or []
            if isinstance(raw, str):
                raw = [raw]
        except Exception as exc:
            logger.warning("[%s] CondorChain: expiration fetch failed: %s", symbol, exc)
            return None

        today = date.today()
        for exp_str in sorted(raw):
            try:
                exp_date = date.fromisoformat(exp_str)
            except (ValueError, TypeError):
                continue
            dte = (exp_date - today).days
            if self.MIN_DTE <= dte <= self.MAX_DTE:
                return exp_str
        return None

    def _pick_short_strike(self, contracts: list, spot: float,
                           option_type: str) -> Optional[dict]:
        """Return contract whose abs(delta) is closest to TARGET_DELTA."""
        best: Optional[dict] = None
        best_diff = 999.0
        for c in contracts:
            delta = c.get("delta")
            if delta is None:
                continue
            diff = abs(abs(float(delta)) - self.TARGET_DELTA)
            if diff < best_diff:
                best, best_diff = c, diff
        return best

    def _find_strike(self, contracts: list, target: float) -> Optional[dict]:
        """Return contract whose strike is within $0.51 of target."""
        for c in contracts:
            if abs(float(c.get("strike", 0)) - target) < 0.51:
                return c
        return None

    def _mid(self, contract: dict) -> float:
        bid = float(contract.get("bid") or 0)
        ask = float(contract.get("ask") or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        return 0.0

    def _dte(self, expiration: str) -> int:
        try:
            exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
            return max(0, (exp_date - date.today()).days)
        except (ValueError, TypeError):
            return 0
