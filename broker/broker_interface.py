"""
broker/broker_interface.py — Abstract broker interface for leap_bot.

All broker adapters implement BrokerInterface so strategy logic never
touches raw HTTP.  OptionContract supports dict-style access for
compatibility with chain/leap_chain.py helpers.

LEAP execution is buy-side (long calls):
    buy_to_open_call()   — enter a LEAP position (pay debit)
    sell_to_close_call() — exit a LEAP position (collect credit)

Market data adapters (TradierClient) raise NotImplementedError on
execution methods.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_LEGACY_KEY_MAP: Dict[str, str] = {
    "type":          "option_type",
    "_delta_source": "delta_source",
    "_price_source": "price_source",
}


@dataclass
class OptionContract:
    """
    Normalised option contract returned by any BrokerInterface.

    Supports both attribute access (c.delta) and dict-style access
    (c["delta"], c.get("mid", 0.0)) for compatibility with leap_chain.py.
    """
    symbol:             str
    strike:             float
    expiration_date:    date
    option_type:        str             # "call" (LEAPs are always calls)
    delta:              Optional[float]
    bid:                Optional[float]
    ask:                Optional[float]
    mid:                Optional[float]
    implied_volatility: Optional[float]
    open_interest:      int
    delta_source:       str = "live"    # "live", "model", "bs", "none"
    price_source:       str = "live"    # "live", "close", "bs", "none"

    def __getitem__(self, key: str):
        mapped = _LEGACY_KEY_MAP.get(key, key)
        try:
            return getattr(self, mapped)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict:
        return {
            "symbol":             self.symbol,
            "strike":             self.strike,
            "expiration_date":    self.expiration_date,
            "type":               self.option_type,
            "delta":              self.delta,
            "bid":                self.bid,
            "ask":                self.ask,
            "mid":                self.mid,
            "implied_volatility": self.implied_volatility,
            "open_interest":      self.open_interest,
            "_delta_source":      self.delta_source,
            "_price_source":      self.price_source,
        }


@dataclass
class OrderResult:
    """
    Normalised order result returned by any BrokerInterface execution method.
    """
    order_id:     str
    status:       str
    filled_price: float
    symbol:       str
    qty:          int

    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def get(self, key: str, default=None):
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict:
        return {
            "order_id":     self.order_id,
            "status":       self.status,
            "filled_price": self.filled_price,
            "symbol":       self.symbol,
            "qty":          self.qty,
        }


class BrokerInterface(ABC):
    """
    Abstract base for all leap_bot broker adapters.

    Market data
    -----------
        get_latest_price()
        get_historical_closes()
        get_option_chain()

    Account / positions
    -------------------
        get_account()
        get_positions()
        get_open_orders()

    LEAP execution (long calls)
    ---------------------------
        buy_to_open_call()    — enter LEAP (pay debit)
        sell_to_close_call()  — exit LEAP (collect credit)

    Data-only adapters (TradierClient) raise NotImplementedError on
    execution methods.
    """

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        """Current mid-price for an equity symbol."""

    @abstractmethod
    def get_historical_closes(self, symbol: str, days: int = 60) -> List[float]:
        """Daily closing prices oldest-first, up to *days* bars."""

    @abstractmethod
    def get_option_chain(
        self,
        symbol: str,
        option_type: str = "call",
        min_dte: int = 365,
        max_dte: int = 730,
        underlying_price: Optional[float] = None,
        historical_vol: Optional[float] = None,
    ) -> List[OptionContract]:
        """
        Return call contracts within the DTE window.
        For LEAPs: min_dte=365, max_dte=730.
        """

    @abstractmethod
    def get_account(self) -> dict:
        """Return account info: buying_power, cash, portfolio_value."""

    @abstractmethod
    def get_positions(self) -> Dict[str, dict]:
        """Return current positions keyed by symbol."""

    @abstractmethod
    def get_open_orders(self) -> List[dict]:
        """Return all open orders."""

    @abstractmethod
    def buy_to_open_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Buy to open a long call (LEAP entry).
        limit_price defaults to ask if None.
        """

    @abstractmethod
    def sell_to_close_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Sell to close a long call (LEAP exit).
        limit_price defaults to bid if None.
        """
