"""
broker/alpaca_client.py — Alpaca broker adapter for leap_bot.

Wraps alpaca-py for LEAP execution (buy-to-open / sell-to-close long calls)
and market data.  Always uses paper=True unless explicitly overridden.

LEAP-specific differences vs wheel_bot
---------------------------------------
- buy_to_open_call()   — BUY side, pays debit
- sell_to_close_call() — SELL side, collects credit
- get_option_chain() defaults to DTE 365–730

Environment variables
---------------------
    ALPACA_API_KEY     — paper or live key
    ALPACA_API_SECRET  — paper or live secret
    ALPACA_BASE_URL    — https://paper-api.alpaca.markets  (paper, default)
                         https://api.alpaca.markets        (live — DO NOT USE YET)
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

from broker.broker_interface import BrokerInterface, OptionContract, OrderResult

logger = logging.getLogger(__name__)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        GetOptionContractsRequest,
        LimitOrderRequest,
        MarketOrderRequest,
    )
    from alpaca.trading.enums import (
        AssetClass,
        OrderSide,
        OrderType,
        TimeInForce,
        ContractType,
    )
    from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
    from alpaca.data.requests import (
        StockBarsRequest,
        StockLatestQuoteRequest,
        OptionSnapshotRequest,
    )
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed — running in MOCK mode. pip install alpaca-py")

# Spread backstop — LEAP spreads are wider, so we use 0.10 here vs 0.05 in wheel_bot
MAX_BID_ASK_SPREAD_PCT: float = 0.10

# Mock LEAP prices for paper testing without live API keys
_MOCK_PRICES = {
    "QQQ": 480.0, "SPY": 535.0, "AAPL": 205.0, "MSFT": 420.0,
    "NVDA": 900.0, "AMZN": 185.0, "GOOGL": 175.0, "META": 520.0,
    "IWM": 215.0, "AMD": 155.0,
}


class AlpacaError(Exception):
    pass


class AlpacaBroker(BrokerInterface):
    """
    Alpaca execution + market data broker for LEAP positions.

    paper=True routes all orders to paper-api.alpaca.markets.
    Never set paper=False until sandbox testing is complete.
    """

    def __init__(self, paper: bool = True) -> None:
        self.paper   = paper
        self._key    = os.environ.get("ALPACA_API_KEY", "")
        self._secret = os.environ.get("ALPACA_API_SECRET", "")
        self._bs_fallback_symbols: Set[str] = set()

        if not _ALPACA_AVAILABLE or not self._key or not self._secret:
            if not _ALPACA_AVAILABLE:
                logger.warning("alpaca-py not installed — MOCK mode")
            else:
                logger.warning(
                    "ALPACA_API_KEY/ALPACA_API_SECRET not set — MOCK mode "
                    "(set keys in .env to enable live paper trading)"
                )
            self._trading     = None
            self._data        = None
            self._option_data = None
            return

        self._trading = TradingClient(
            api_key=self._key, secret_key=self._secret, paper=paper,
        )
        self._data = StockHistoricalDataClient(
            api_key=self._key, secret_key=self._secret,
        )
        self._option_data = OptionHistoricalDataClient(
            api_key=self._key, secret_key=self._secret,
        )
        logger.info("AlpacaBroker initialised (paper=%s)", paper)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        if not self._trading:
            return {"buying_power": 100_000.0, "cash": 100_000.0, "portfolio_value": 100_000.0}
        acct = self._trading.get_account()
        return {
            "buying_power":    float(acct.buying_power),
            "cash":            float(acct.cash),
            "portfolio_value": float(acct.portfolio_value),
            "equity":          float(acct.equity),
        }

    def get_positions(self) -> Dict[str, dict]:
        if not self._trading:
            return {}
        positions = self._trading.get_all_positions()
        return {
            p.symbol: {
                "qty":        float(p.qty),
                "avg_entry":  float(p.avg_entry_price),
                "market_val": float(p.market_value),
                "unrealized": float(p.unrealized_pl),
            }
            for p in positions
        }

    def get_open_orders(self) -> List[dict]:
        if not self._trading:
            return []
        orders = self._trading.get_orders()
        return [
            {
                "id":     str(o.id),
                "symbol": o.symbol,
                "side":   str(o.side),
                "qty":    float(o.qty),
                "status": str(o.status),
            }
            for o in orders
        ]

    # ── Market data ───────────────────────────────────────────────────────────

    def get_latest_price(self, symbol: str) -> float:
        if not self._data:
            return _MOCK_PRICES.get(symbol, 100.0)
        req   = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        quote = self._data.get_stock_latest_quote(req)[symbol]
        bid   = float(quote.bid_price or 0)
        ask   = float(quote.ask_price or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        if ask > 0:
            return round(ask, 4)
        if bid > 0:
            return round(bid, 4)
        closes = self.get_historical_closes(symbol, days=1)
        return closes[-1] if closes else 0.0

    def get_historical_closes(self, symbol: str, days: int = 60) -> List[float]:
        if not self._data:
            return [_MOCK_PRICES.get(symbol, 100.0)] * days
        end   = datetime.utcnow()
        start = end - timedelta(days=days + 10)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start, end=end,
            feed=DataFeed.IEX,
        )
        bars = self._data.get_stock_bars(req)[symbol]
        return [float(b.close) for b in bars][-days:]

    def get_option_mid(self, option_symbol: str) -> Optional[float]:
        """Current mid-price for a single option contract."""
        if not self._option_data:
            return 12.50  # mock stub
        try:
            snaps = self._option_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=[option_symbol])
            )
            snap = snaps.get(option_symbol)
            if snap and snap.latest_quote:
                bid = float(snap.latest_quote.bid_price or 0) or None
                ask = float(snap.latest_quote.ask_price or 0) or None
                if bid and ask:
                    return round((bid + ask) / 2, 4)
        except Exception as exc:
            logger.warning("[%s] get_option_mid failed: %s", option_symbol, exc)
        return None

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
        Return LEAP call contracts within the DTE window.

        Falls back to Black-Scholes delta when Alpaca returns no Greeks.
        Spread-filtered at MAX_BID_ASK_SPREAD_PCT.
        """
        if not self._trading:
            return self._mock_chain(symbol, option_type, min_dte, max_dte, underlying_price)

        today = date.today()
        req   = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status="active",
            expiration_date_gte=date.fromordinal(today.toordinal() + min_dte),
            expiration_date_lte=date.fromordinal(today.toordinal() + max_dte),
            type=ContractType.CALL if option_type == "call" else ContractType.PUT,
            style="american",
        )

        try:
            contracts_page = self._trading.get_option_contracts(req)
        except Exception as exc:
            logger.error("[%s] Alpaca option chain fetch failed: %s", symbol, exc)
            return []

        contracts = list(contracts_page.option_contracts or [])
        if not contracts:
            logger.warning("[%s] Alpaca: no LEAP contracts returned", symbol)
            return []

        # Fetch snapshots for pricing + greeks
        syms = [c.symbol for c in contracts]
        try:
            snaps = self._option_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=syms)
            ) if self._option_data else {}
        except Exception as exc:
            logger.warning("[%s] Alpaca snapshot fetch failed: %s", symbol, exc)
            snaps = {}

        # Compute historical vol for BS fallback
        hist_vol = historical_vol
        if hist_vol is None and underlying_price:
            try:
                closes  = self.get_historical_closes(symbol, days=60)
                hist_vol = _compute_hist_vol(closes)
            except Exception:
                hist_vol = 0.25

        result: List[OptionContract] = []

        for c in contracts:
            snap  = snaps.get(c.symbol)
            bid   = ask = mid = delta = iv = None

            if snap and snap.latest_quote:
                bid = float(snap.latest_quote.bid_price or 0) or None
                ask = float(snap.latest_quote.ask_price or 0) or None
                if bid and ask:
                    mid = round((bid + ask) / 2, 4)

            if snap and snap.greeks:
                delta = float(snap.greeks.delta) if snap.greeks.delta else None
            if snap and snap.implied_volatility:
                iv = float(snap.implied_volatility)

            # BS delta fallback
            delta_src = "live"
            if delta is None and underlying_price and hist_vol:
                exp_date = c.expiration_date if isinstance(c.expiration_date, date) \
                           else date.fromisoformat(str(c.expiration_date))
                T = (exp_date - today).days / 365.0
                delta = _bs_call_delta(
                    underlying_price, float(c.strike_price), T, hist_vol
                )
                delta_src = "bs"
                self._bs_fallback_symbols.add(symbol)

            # BS price fallback
            price_src = "live"
            if mid is None and underlying_price and hist_vol and delta is not None:
                exp_date = c.expiration_date if isinstance(c.expiration_date, date) \
                           else date.fromisoformat(str(c.expiration_date))
                T = (exp_date - today).days / 365.0
                mid = _bs_call_price(
                    underlying_price, float(c.strike_price), T, hist_vol
                )
                price_src = "bs"

            # Spread filter
            if bid and ask and mid:
                spread_pct = (ask - bid) / mid
                if spread_pct > MAX_BID_ASK_SPREAD_PCT:
                    logger.debug(
                        "[%s] Alpaca spread filter: %s spread=%.0f%% — dropped",
                        symbol, c.symbol, spread_pct * 100,
                    )
                    continue

            exp_date = c.expiration_date if isinstance(c.expiration_date, date) \
                       else date.fromisoformat(str(c.expiration_date))

            result.append(OptionContract(
                symbol             = c.symbol,
                strike             = float(c.strike_price),
                expiration_date    = exp_date,
                option_type        = option_type,
                delta              = delta,
                bid                = bid,
                ask                = ask,
                mid                = mid,
                implied_volatility = iv,
                open_interest      = int(getattr(c, "open_interest", 0) or 0),
                delta_source       = delta_src,
                price_source       = price_src,
            ))

        logger.info(
            "[%s] Alpaca: %d LEAP call(s) returned (DTE %d–%d)",
            symbol, len(result), min_dte, max_dte,
        )
        return result

    def _mock_chain(self, symbol, option_type, min_dte, max_dte, underlying_price):
        """Synthetic chain for MOCK mode testing."""
        import math
        from chain.leap_chain import _bs_call_delta as bsd
        spot = underlying_price or _MOCK_PRICES.get(symbol, 100.0)
        today = date.today()
        exp   = date.fromordinal(today.toordinal() + min_dte + 30)
        T     = (exp - today).days / 365.0
        sigma = 0.25
        result = []
        for strike_mult in [0.75, 0.80, 0.85, 0.90, 0.95, 1.00]:
            K   = round(spot * strike_mult, 0)
            d   = bsd(spot, K, T, sigma)
            mid = max(round(spot * strike_mult * 0.15, 2), 1.0)
            result.append(OptionContract(
                symbol=f"{symbol}{exp.strftime('%y%m%d')}C{int(K*1000):08d}",
                strike=K, expiration_date=exp, option_type="call",
                delta=d, bid=round(mid*0.97,2), ask=round(mid*1.03,2), mid=mid,
                implied_volatility=sigma, open_interest=500,
                delta_source="bs", price_source="bs",
            ))
        return result

    def clear_bs_fallback(self):
        self._bs_fallback_symbols.clear()

    # ── LEAP execution (buy-side) ─────────────────────────────────────────────

    def buy_to_open_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Buy to open a long LEAP call.

        Uses a limit order at limit_price if provided, otherwise market order.
        Paper mode only until PAPER_TRADING guard is removed.
        """
        if not self._trading:
            logger.info("[MOCK] BTO %s x%d @ %s", option_symbol, contracts, limit_price)
            return OrderResult(
                order_id="mock-bto-001", status="filled",
                filled_price=limit_price or 0.0,
                symbol=option_symbol, qty=contracts,
            )
        try:
            if limit_price:
                req = LimitOrderRequest(
                    symbol=option_symbol,
                    qty=contracts,
                    side=OrderSide.BUY,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                )
            else:
                req = MarketOrderRequest(
                    symbol=option_symbol,
                    qty=contracts,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            order = self._trading.submit_order(req)
            logger.info(
                "BTO %s x%d — order %s status=%s",
                option_symbol, contracts, order.id, order.status,
            )
            return OrderResult(
                order_id=str(order.id),
                status=str(order.status),
                filled_price=float(order.filled_avg_price or limit_price or 0),
                symbol=option_symbol,
                qty=contracts,
            )
        except Exception as exc:
            raise AlpacaError(f"buy_to_open_call failed: {exc}") from exc

    def sell_to_close_call(
        self,
        option_symbol: str,
        contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Sell to close a long LEAP call.

        Uses a limit order at limit_price if provided, otherwise market order.
        """
        if not self._trading:
            logger.info("[MOCK] STC %s x%d @ %s", option_symbol, contracts, limit_price)
            return OrderResult(
                order_id="mock-stc-001", status="filled",
                filled_price=limit_price or 0.0,
                symbol=option_symbol, qty=contracts,
            )
        try:
            if limit_price:
                req = LimitOrderRequest(
                    symbol=option_symbol,
                    qty=contracts,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                )
            else:
                req = MarketOrderRequest(
                    symbol=option_symbol,
                    qty=contracts,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            order = self._trading.submit_order(req)
            logger.info(
                "STC %s x%d — order %s status=%s",
                option_symbol, contracts, order.id, order.status,
            )
            return OrderResult(
                order_id=str(order.id),
                status=str(order.status),
                filled_price=float(order.filled_avg_price or limit_price or 0),
                symbol=option_symbol,
                qty=contracts,
            )
        except Exception as exc:
            raise AlpacaError(f"sell_to_close_call failed: {exc}") from exc

    # ── Paper order placement ─────────────────────────────────────────────────

    def place_order(self, preview) -> OrderResult:
        """
        Paper-mode order placement.

        Delegates to buy_to_open_call() using the mid price from the preview.
        No real money.  Returns a mock OrderResult when Alpaca keys are absent.
        """
        fill_price = preview.mid or 0.0
        return self.buy_to_open_call(
            preview.option_symbol,
            contracts=preview.contracts,
            limit_price=fill_price,
        )


# ── BS helpers (used internally for fallback delta/price) ─────────────────────

import math
from statistics import stdev as _stdev


def _compute_hist_vol(closes: List[float], trading_days: int = 252) -> float:
    if len(closes) < 10:
        return 0.25
    log_returns = [
        math.log(closes[i] / closes[i-1])
        for i in range(1, len(closes))
        if closes[i-1] > 0
    ]
    return _stdev(log_returns) * math.sqrt(trading_days) if log_returns else 0.25


def _bs_call_delta(S: float, K: float, T: float, sigma: float,
                   r: float = 0.045) -> Optional[float]:
    from statistics import NormalDist
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return round(NormalDist().cdf(d1), 4)
    except Exception:
        return None


def _bs_call_price(S: float, K: float, T: float, sigma: float,
                   r: float = 0.045) -> Optional[float]:
    from statistics import NormalDist
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        nd  = NormalDist()
        d1  = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2  = d1 - sigma * math.sqrt(T)
        val = S * nd.cdf(d1) - K * math.exp(-r * T) * nd.cdf(d2)
        return max(round(val, 4), 0.01)
    except Exception:
        return None
