"""
broker/preview.py — LEAP order preview logic.

build_preview(broker, symbol, strike, expiration, contracts=1)
    → PreviewResult

Fetches the option chain for the given symbol, finds the contract matching
the requested strike + expiration (with fuzzy fallback to nearest strike),
assembles pricing/Greeks, runs the warning checklist, and returns a
PreviewResult.

PreviewResult is the shared currency between /leap_preview, /enter_paper,
and /broker_preview.  It is also what gets passed to place_order().

Warning checklist
-----------------
W1  No live bid/ask — pricing is theoretical (BS or close)
W2  Greeks from Black-Scholes — delta/IV are estimates
W3  Low open interest (< LEAP_MIN_OI)
W4  Wide bid/ask spread (> LEAP_MAX_SPREAD_PCT)
W5  High extrinsic value (> LEAP_MAX_EXTRINSIC of mid)
W6  Contract not found at requested strike — using nearest available
W7  No matching expiration — using nearest available in window
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── PreviewResult ─────────────────────────────────────────────────────────────

@dataclass
class PreviewResult:
    # Identity
    underlying_symbol: str
    option_symbol:     str
    strike:            float
    expiration:        str          # YYYY-MM-DD
    option_type:       str          # "call"
    side:              str          # "buy"
    contracts:         int

    # Pricing
    bid:            Optional[float]
    ask:            Optional[float]
    mid:            Optional[float]
    estimated_cost: float           # mid * 100 * contracts (or 0)

    # Greeks / liquidity
    delta:          Optional[float]
    iv:             Optional[float]
    open_interest:  Optional[int]

    # Quality metadata
    delta_source:   str             # "live", "bs", "none"
    price_source:   str             # "live", "bs", "none"
    warnings:       List[str] = field(default_factory=list)

    # Context
    mode:           str = "paper"   # "paper", "sandbox", "live"
    broker_name:    str = "tradier"

    # Screener data (filled by caller when available)
    leap_score:     Optional[float] = None
    trend_score:    Optional[float] = None
    risk_rating:    Optional[str]   = None
    breakeven:      Optional[float] = None
    target_exit:    Optional[float] = None
    stop_loss:      Optional[float] = None


# ── OCC symbol helper ─────────────────────────────────────────────────────────

def _occ_symbol(underlying: str, exp: date, strike: float, option_type: str) -> str:
    """
    Build an OCC option symbol.
    Format: {underlying padded to 6}{yymmdd}{C/P}{strike*1000 zero-padded to 8}

    e.g. QQQ 480C 2027-01-15 → "QQQ   270115C00480000"
    """
    padded    = underlying.upper().ljust(6)
    date_str  = exp.strftime("%y%m%d")
    cp        = "C" if option_type.lower() == "call" else "P"
    strike_i  = int(round(strike * 1000))
    return f"{padded}{date_str}{cp}{strike_i:08d}"


# ── Core builder ──────────────────────────────────────────────────────────────

def build_preview(
    broker,
    symbol:      str,
    strike:      float,
    expiration:  str,          # YYYY-MM-DD from screener rec
    option_type: str = "call",
    side:        str = "buy",
    contracts:   int = 1,
) -> PreviewResult:
    """
    Fetch the option chain and assemble a PreviewResult for the given
    symbol/strike/expiration.

    Falls back gracefully when the exact strike or expiration is unavailable.
    Generates warnings for any data quality issues.
    """
    import config

    # Determine mode + broker name from config
    mode        = config.BROKER_MODE.lower()
    broker_name = type(broker).__name__.replace("Broker", "").replace("Client", "").lower()

    # Canonical display mode
    if mode in ("paper", "single"):
        display_mode = "paper"
    elif mode == "sandbox":
        display_mode = "sandbox"
    else:
        display_mode = mode

    try:
        exp_date = date.fromisoformat(expiration)
    except ValueError:
        exp_date = date.today()

    # ── Fetch chain ───────────────────────────────────────────────────────────
    try:
        price = broker.get_latest_price(symbol)
        chain = broker.get_option_chain(
            symbol, option_type,
            min_dte=config.EXP_RANGE_MIN_DAYS,
            max_dte=config.EXP_RANGE_MAX_DAYS,
            underlying_price=price,
        )
    except Exception as exc:
        logger.warning("[%s] build_preview chain fetch failed: %s", symbol, exc)
        chain = []
        price = 0.0

    # Convert OptionContract objects to dicts for uniform access
    chain_dicts = []
    for c in chain:
        d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
        chain_dicts.append(d)

    # ── Find best matching contract ───────────────────────────────────────────
    warnings: List[str] = []
    contract  = None
    used_exp  = expiration
    used_strike = strike

    if chain_dicts:
        # Try exact strike + expiration match first
        exact = [
            c for c in chain_dicts
            if abs(c.get("strike", 0) - strike) < 0.01
            and str(c.get("expiration_date", "")).startswith(expiration[:7])
        ]
        if exact:
            contract = exact[0]
        else:
            # Fall back to nearest expiration in window
            exp_groups: dict = {}
            for c in chain_dicts:
                e = str(c.get("expiration_date", ""))[:10]
                exp_groups.setdefault(e, []).append(c)

            if exp_groups:
                # nearest expiration to the requested one
                nearest_exp = min(
                    exp_groups.keys(),
                    key=lambda e: abs((date.fromisoformat(e) - exp_date).days),
                )
                if nearest_exp != expiration[:10]:
                    warnings.append(
                        f"W7 Requested expiration {expiration[:10]} not in chain "
                        f"— using nearest: {nearest_exp}"
                    )
                    used_exp = nearest_exp

                group = exp_groups[nearest_exp]
                # nearest strike
                contract = min(group, key=lambda c: abs(c.get("strike", 0) - strike))
                if contract and abs(contract.get("strike", 0) - strike) > 0.5:
                    warnings.append(
                        f"W6 Strike ${strike:.0f} not available "
                        f"— using nearest: ${contract.get('strike', 0):.0f}"
                    )
                    used_strike = contract.get("strike", strike)

    # ── Extract pricing + Greeks ──────────────────────────────────────────────
    bid = ask = mid = delta = iv = None
    oi  = 0
    delta_src = price_src = "none"
    # Bug fix: use used_exp (after W7 fallback), not the original exp_date
    used_exp_date = date.fromisoformat(str(used_exp)[:10]) if used_exp != expiration else exp_date
    opt_symbol = _occ_symbol(symbol, used_exp_date, used_strike, option_type)

    if contract:
        bid       = contract.get("bid")
        ask       = contract.get("ask")
        mid       = contract.get("mid")
        delta     = contract.get("delta")
        iv        = contract.get("implied_volatility")
        oi        = contract.get("open_interest") or 0
        delta_src = contract.get("_delta_source", "none")
        price_src = contract.get("_price_source", "none")
        opt_symbol = contract.get("symbol") or opt_symbol

    if not mid and bid and ask:
        mid = round((bid + ask) / 2, 4)
    if not mid and ask:
        mid = ask

    estimated_cost = round((mid or 0) * 100 * contracts, 2)

    # ── Warnings ──────────────────────────────────────────────────────────────
    if price_src != "live" or (bid is None and ask is None):
        warnings.append("W1 No live bid/ask — pricing is theoretical")

    if delta_src in ("bs", "model", "none"):
        warnings.append(
            "W2 Delta from Black-Scholes estimate — not live Greeks"
            if delta_src == "bs"
            else "W2 No Greeks available for this contract"
        )

    if oi is not None and oi < config.LEAP_MIN_OI:
        warnings.append(
            f"W3 Low open interest ({oi}) — liquidity may be poor"
        )

    if bid and ask and mid:
        spread_pct = (ask - bid) / mid
        if spread_pct > config.LEAP_MAX_SPREAD_PCT:
            warnings.append(
                f"W4 Wide spread ({spread_pct:.0%}) — fill quality may be poor"
            )

    if mid and price and used_strike:
        from chain.leap_chain import extrinsic_value
        ext = extrinsic_value(mid, used_strike, price)
        ext_pct = ext / mid if mid else 0
        if ext_pct > config.LEAP_MAX_EXTRINSIC:
            warnings.append(
                f"W5 High extrinsic ${ext:.2f} ({ext_pct:.0%} of mid) "
                f"— paying too much time premium"
            )

    if not chain_dicts:
        warnings.append("W8 No chain data returned — broker may be in MOCK mode")

    # ── Compute derived fields ────────────────────────────────────────────────
    # All three stored as per-share values so callers never need to re-scale.
    # Per-contract dollar cost = value * 100 * contracts — done at display time.
    be          = round(used_strike + (mid or 0), 2) if mid else None
    target_exit = round((mid or 0) * (1 + config.LEAP_TARGET_GAIN), 2)   # per-share
    stop_loss   = round((mid or 0) * (1 - config.LEAP_MAX_LOSS),  2)     # per-share

    return PreviewResult(
        underlying_symbol = symbol.upper(),
        option_symbol     = opt_symbol,
        strike            = used_strike,
        expiration        = str(used_exp)[:10],
        option_type       = option_type,
        side              = side,
        contracts         = contracts,
        bid               = bid,
        ask               = ask,
        mid               = mid,
        estimated_cost    = estimated_cost,
        delta             = delta,
        iv                = iv,
        open_interest     = oi,
        delta_source      = delta_src,
        price_source      = price_src,
        warnings          = warnings,
        mode              = display_mode,
        broker_name       = broker_name,
        breakeven         = be,
        target_exit       = target_exit,
        stop_loss         = stop_loss,
    )
