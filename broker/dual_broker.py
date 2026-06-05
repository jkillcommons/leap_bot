"""
broker/dual_broker.py — Dual-broker routing shim for leap_bot.

Kept for import compatibility.  Since TradierClient now handles both
market data and execution, DualBroker simply passes all calls through
to the single underlying broker.

Usage
-----
    from broker.tradier_client import TradierClient
    from broker.dual_broker import DualBroker

    broker = DualBroker(data_broker=TradierClient(), execution_broker=TradierClient())
    # or more simply: just use TradierClient() directly.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from broker.broker_interface import BrokerInterface, OptionContract, OrderResult

logger = logging.getLogger(__name__)


class DualBroker(BrokerInterface):
    """
    Routes market-data calls to one broker, execution calls to another.

    Parameters
    ----------
    data_broker      : Broker for price / option-chain queries (TradierClient).
    execution_broker : Broker for account, position, and order methods (AlpacaBroker).
    """

    def __init__(
        self,
        data_broker: BrokerInterface,
        execution_broker: BrokerInterface,
    ) -> None:
        self.data      = data_broker
        self.execution = execution_broker
        logger.info(
            "DualBroker initialised — data: %s  execution: %s",
            type(data_broker).__name__,
            type(execution_broker).__name__,
        )

    # ── Market data → self.data ───────────────────────────────────────────────

    def get_latest_price(self, symbol: str) -> float:
        return self.data.get_latest_price(symbol)

    def get_historical_closes(self, symbol: str, days: int = 60) -> List[float]:
        return self.data.get_historical_closes(symbol, days)

    def get_option_chain(
        self,
        symbol: str,
        option_type: str = "call",
        min_dte: int = 365,
        max_dte: int = 730,
        underlying_price: Optional[float] = None,
        historical_vol: Optional[float] = None,
    ) -> List[OptionContract]:
        return self.data.get_option_chain(
            symbol, option_type,
            min_dte=min_dte, max_dte=max_dte,
            underlying_price=underlying_price,
            historical_vol=historical_vol,
        )

    # ── Account / positions → self.execution ─────────────────────────────────

    def get_account(self) -> dict:
        return self.execution.get_account()

    def get_positions(self) -> Dict[str, dict]:
        return self.execution.get_positions()

    def get_open_orders(self) -> List[dict]:
        return self.execution.get_open_orders()

    # ── LEAP execution → self.execution ──────────────────────────────────────

    def buy_to_open_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        return self.execution.buy_to_open_call(
            option_symbol, contracts=contracts, limit_price=limit_price
        )

    def sell_to_close_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        return self.execution.sell_to_close_call(
            option_symbol, contracts=contracts, limit_price=limit_price
        )
