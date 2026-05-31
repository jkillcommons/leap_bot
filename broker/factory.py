"""
broker/factory.py — Broker factory and singleton for leap_bot.

get_broker()    → cached BrokerInterface for the current BROKER_MODE
make_broker()   → always creates a fresh instance (use for tests)
reset_broker()  → clear the cache (call after .env changes in tests)

BROKER_MODE values
------------------
  paper    — AlpacaBroker paper=True (mock if no keys set)
             No real API calls for execution.  Chain data from Alpaca
             paper feed or MOCK mode if ALPACA keys absent.

  sandbox  — TradierClient pointed at sandbox.tradier.com
             get_option_chain() uses live Tradier sandbox data.
             place_order() submits real sandbox orders (no money).
             Requires: TRADIER_API_TOKEN, TRADIER_ACCOUNT_ID

  dual     — TradierClient for data, AlpacaBroker for execution.
             Requires both sets of credentials.

  single   — Alias for paper (backwards-compat).
  alpaca   — Alias for paper.
  tradier  — Alias for sandbox (data-only, no order placement).
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
    Create and return a fresh BrokerInterface instance.

    Falls back to config.py values for unspecified parameters.
    Raises RuntimeError if PAPER_TRADING=False (live-mode guard).
    """
    import config

    _mode  = (mode or config.BROKER_MODE).lower()
    _paper = paper if paper is not None else config.PAPER_TRADING

    if not _paper:
        raise RuntimeError(
            "PAPER_TRADING is False. "
            "Remove this guard only after completing sandbox testing."
        )

    # ── sandbox ───────────────────────────────────────────────────────────────
    # TradierClient pointed at sandbox.tradier.com with order placement enabled.
    if _mode in ("sandbox", "tradier"):
        from broker.tradier_client import TradierClient
        logger.info("Broker: TradierClient (sandbox)")
        return TradierClient()

    # ── dual ──────────────────────────────────────────────────────────────────
    if _mode == "dual":
        from broker.tradier_client import TradierClient
        from broker.alpaca_client  import AlpacaBroker
        from broker.dual_broker    import DualBroker
        tradier = TradierClient()
        alpaca  = AlpacaBroker(paper=True)
        logger.info("Broker: DualBroker (Tradier data / Alpaca paper execution)")
        return DualBroker(data_broker=tradier, execution_broker=alpaca)

    # ── paper / single / alpaca (default) ─────────────────────────────────────
    from broker.alpaca_client import AlpacaBroker
    logger.info("Broker: AlpacaBroker (paper=%s)", _paper)
    return AlpacaBroker(paper=_paper)
