"""
broker/factory.py — Broker factory and singleton for leap_bot.

get_broker()    → cached BrokerInterface for the current BROKER_MODE
make_broker()   → always creates a fresh instance (use for tests)
reset_broker()  → clear the cache (call after .env changes in tests)

TradierClient is the sole broker for all modes.  BROKER_MODE is still
read so callers can distinguish sandbox vs (future) live, but the
returned instance is always a TradierClient.

BROKER_MODE values
------------------
  sandbox  — TradierClient → sandbox.tradier.com (default)
             get_option_chain() + place_order() use sandbox.
             Requires: TRADIER_API_TOKEN, TRADIER_ACCOUNT_ID

  tradier  — Alias for sandbox.
  paper    — Alias for sandbox (backwards-compat).
  single   — Alias for sandbox (backwards-compat).
  dual     — Alias for sandbox (Tradier now handles both data and execution).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level singleton cache
_broker_cache: Optional[object] = None
_broker_mode_cache: Optional[str] = None


def get_broker():
    """
    Return the cached broker instance, creating it if necessary.

    Re-creates if BROKER_MODE has changed since last call (e.g. after
    env-var reload).  Thread-safety is not a concern — single-threaded
    Telegram polling loop.
    """
    global _broker_cache, _broker_mode_cache
    import config
    current_mode = config.BROKER_MODE.lower()
    if _broker_cache is None or current_mode != _broker_mode_cache:
        _broker_cache = make_broker()
        _broker_mode_cache = current_mode
    return _broker_cache


def reset_broker():
    """Clear the singleton cache.  Call after env changes in tests."""
    global _broker_cache, _broker_mode_cache
    _broker_cache = None
    _broker_mode_cache = None


def make_broker(
    mode: Optional[str] = None,
    data: Optional[str] = None,
    paper: Optional[bool] = None,
):
    """
    Create and return a fresh TradierClient instance.

    Falls back to config.py values for unspecified parameters.
    Raises RuntimeError if PAPER_TRADING=False (live-mode guard).
    """
    import config

    _paper = paper if paper is not None else config.PAPER_TRADING

    if not _paper:
        raise RuntimeError(
            "PAPER_TRADING is False. "
            "Remove this guard only after completing sandbox testing."
        )

    from broker.tradier_client import TradierClient
    _mode = (mode or config.BROKER_MODE).lower()
    logger.info("Broker: TradierClient (mode=%s)", _mode)
    return TradierClient()
