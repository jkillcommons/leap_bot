"""
broker/tradier_client.py — Tradier broker adapter for leap_bot.

Implements BrokerInterface for market-data AND account/execution methods.

Key LEAP differences vs wheel_bot
-----------------------------------
- DTE window defaults to 365–730 days (12–24 months)
- Only "call" option_type is fetched
- Spread filter is wider (0.10) — LEAP spreads are naturally larger
- Greeks (delta) must be >= SUGGESTED_DELTA_LOW (0.70) for deep ITM

Environment variables
---------------------
    TRADIER_API_TOKEN   : Bearer token (sandbox or live)
    TRADIER_ACCOUNT_ID  : Account ID for order placement / account queries
    TRADIER_PRODUCTION  : "true" to use live API (default: sandbox)

Sandbox : https://sandbox.tradier.com/v1
Live    : https://api.tradier.com/v1
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Dict, List, Optional

from broker.broker_interface import BrokerInterface, OptionContract, OrderResult

logger = logging.getLogger(__name__)

_SANDBOX_URL = "https://sandbox.tradier.com/v1"
_LIVE_URL    = "https://api.tradier.com/v1"

# LEAP spread filter — wider than wheel_bot's 0.05 because deep ITM LEAP
# bid/ask spreads are structurally wider.  Still screens out illiquid trash.
_MAX_SPREAD_PCT: float = 0.10


class TradierError(Exception):
    pass


class TradierClient(BrokerInterface):
    """
    Tradier market-data broker.  No order execution.

    Parameters
    ----------
    token      : API bearer token. Defaults to TRADIER_API_TOKEN env var.
    production : Use live API when True. Defaults to sandbox.
    """

    def __init__(self, token: Optional[str] = None, production: bool = False) -> None:
        self._token = (token or os.environ.get("TRADIER_API_TOKEN", "")).strip()
        if not self._token:
            raise TradierError(
                "TRADIER_API_TOKEN not set. "
                "Get a free sandbox token at https://developer.tradier.com"
            )
        _prod_env = os.environ.get("TRADIER_PRODUCTION", "false").lower() == "true"
        self._base = _LIVE_URL if (production or _prod_env) else _SANDBOX_URL
        mode = "production" if (production or _prod_env) else "sandbox"
        logger.info("TradierClient initialised (%s)", mode)

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> dict:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode())
                raise TradierError(f"Tradier HTTP {exc.code}: {err}") from exc
            except (json.JSONDecodeError, AttributeError):
                raise TradierError(f"Tradier HTTP {exc.code}") from exc
        except Exception as exc:
            raise TradierError(f"Tradier request failed: {exc}") from exc

    # ── Market data ───────────────────────────────────────────────────────────

    def get_latest_price(self, symbol: str) -> float:
        data  = self._get("/markets/quotes", {"symbols": symbol, "greeks": "false"})
        quote = data.get("quotes", {}).get("quote") or {}
        if isinstance(quote, list):
            quote = next((q for q in quote if q.get("symbol") == symbol), {})

        last  = quote.get("last")
        bid   = quote.get("bid")
        ask   = quote.get("ask")
        close = quote.get("close") or quote.get("prevclose")

        if last and float(last) > 0:
            return round(float(last), 4)
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return round((float(bid) + float(ask)) / 2, 4)
        if close and float(close) > 0:
            return round(float(close), 4)

        logger.warning("[%s] Tradier: no usable price returned", symbol)
        return 0.0

    def get_historical_closes(self, symbol: str, days: int = 60) -> List[float]:
        end   = date.today()
        start = end - timedelta(days=days + 15)
        data  = self._get("/markets/history", {
            "symbol": symbol, "interval": "daily",
            "start": start.isoformat(), "end": end.isoformat(),
        })
        days_data = (data.get("history") or {}).get("day") or []
        if isinstance(days_data, dict):
            days_data = [days_data]
        closes = [float(d["close"]) for d in days_data if d.get("close") is not None]
        return closes[-days:] if len(closes) > days else closes

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
        Return Tradier LEAP call contracts for *symbol* in the DTE window.

        Steps:
          1. Fetch available expirations
          2. Filter to [min_dte, max_dte] from today
          3. Fetch chain with greeks=true for each valid expiration
          4. Filter to option_type (always "call" for LEAPs)
          5. Apply spread filter
          6. Return List[OptionContract]
        """
        today = date.today()

        # Step 1: expirations
        exp_data = self._get("/markets/options/expirations", {
            "symbol": symbol, "includeAllRoots": "true", "strikes": "false",
        })
        raw_exps = (exp_data.get("expirations") or {}).get("date") or []
        if isinstance(raw_exps, str):
            raw_exps = [raw_exps]

        # Step 2: DTE filter
        valid_exps: List[str] = []
        for exp_str in raw_exps:
            try:
                exp_date = date.fromisoformat(exp_str)
            except (ValueError, TypeError):
                continue
            days_out = (exp_date - today).days
            if min_dte <= days_out <= max_dte:
                valid_exps.append(exp_str)

        if not valid_exps:
            logger.warning(
                "[%s] Tradier: no expirations in LEAP DTE window %d–%d days",
                symbol, min_dte, max_dte,
            )
            return []

        logger.info(
            "[%s] Tradier: %d expiration(s) in window %d–%d days: %s",
            symbol, len(valid_exps), min_dte, max_dte, ", ".join(valid_exps),
        )

        # Steps 3–6: fetch chains, filter, build OptionContract list
        result: List[OptionContract] = []

        for exp_str in valid_exps:
            chain_data = self._get("/markets/options/chains", {
                "symbol": symbol, "expiration": exp_str, "greeks": "true",
            })
            options = (chain_data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            exp_date = date.fromisoformat(exp_str)

            for opt in options:
                if (opt.get("option_type") or "").lower() != option_type.lower():
                    continue

                bid_raw = opt.get("bid")
                ask_raw = opt.get("ask")
                bid = float(bid_raw) if bid_raw is not None else None
                ask = float(ask_raw) if ask_raw is not None else None
                if bid is not None and bid <= 0:
                    bid = None
                if ask is not None and ask <= 0:
                    ask = None
                mid = round((bid + ask) / 2, 4) if bid and ask else None

                # Spread filter
                if bid and ask and mid:
                    spread_pct = (ask - bid) / mid
                    if spread_pct > _MAX_SPREAD_PCT:
                        logger.debug(
                            "[%s] spread filter: strike=%.2f spread=%.0f%% — dropped",
                            symbol, float(opt.get("strike", 0)), spread_pct * 100,
                        )
                        continue

                greeks    = opt.get("greeks") or {}
                raw_delta = greeks.get("delta")
                delta     = float(raw_delta) if raw_delta is not None else None
                raw_iv    = greeks.get("mid_iv") or greeks.get("smv_vol")
                iv        = float(raw_iv) if raw_iv is not None else None

                strike_raw = opt.get("strike")
                if strike_raw is None:
                    continue

                result.append(OptionContract(
                    symbol             = opt.get("symbol", ""),
                    strike             = float(strike_raw),
                    expiration_date    = exp_date,
                    option_type        = option_type,
                    delta              = delta,
                    bid                = bid,
                    ask                = ask,
                    mid                = mid,
                    implied_volatility = iv,
                    open_interest      = int(opt.get("open_interest") or 0),
                    delta_source       = "live" if delta is not None else "none",
                    price_source       = "live" if mid  is not None else "none",
                ))

        logger.info(
            "[%s] Tradier: %d %s LEAP contract(s) returned",
            symbol, len(result), option_type,
        )
        return result

    # ── HTTP POST helper ──────────────────────────────────────────────────────

    def _post(self, path: str, data: Dict[str, str]) -> dict:
        """POST form-encoded data to the Tradier API."""
        url     = self._base + path
        payload = urllib.parse.urlencode(data).encode("utf-8")
        req     = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization":  f"Bearer {self._token}",
                "Accept":         "application/json",
                "Content-Type":   "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode())
                raise TradierError(f"Tradier HTTP {exc.code}: {err}") from exc
            except (json.JSONDecodeError, AttributeError):
                raise TradierError(f"Tradier HTTP {exc.code}") from exc
        except Exception as exc:
            raise TradierError(f"Tradier POST failed: {exc}") from exc

    # ── Sandbox order placement ───────────────────────────────────────────────

    def place_order(self, preview) -> OrderResult:
        """
        Submit a sandbox buy-to-open order to Tradier.

        Requires TRADIER_ACCOUNT_ID in environment.
        Only valid on sandbox.tradier.com (TRADIER_PRODUCTION must be false).

        Parameters
        ----------
        preview : PreviewResult from broker/preview.py
        """
        account_id = os.environ.get("TRADIER_ACCOUNT_ID", "").strip()
        if not account_id:
            raise TradierError(
                "TRADIER_ACCOUNT_ID not set. "
                "Find your account ID at https://developer.tradier.com/user/profile"
            )

        if not preview.mid:
            raise TradierError("Cannot place order — no mid price in preview")

        # Tradier expects limit price as a string with 2 decimal places
        limit_price = f"{preview.mid:.2f}"

        payload = {
            "class":         "option",
            "symbol":        preview.underlying_symbol,
            "option_symbol": preview.option_symbol,
            "side":          "buy_to_open",
            "quantity":      str(preview.contracts),
            "type":          "limit",
            "duration":      "day",
            "price":         limit_price,
        }

        logger.info(
            "[SANDBOX] Submitting order: %s x%d @ $%s",
            preview.option_symbol, preview.contracts, limit_price,
        )

        resp = self._post(f"/accounts/{account_id}/orders", payload)

        order = resp.get("order") or {}
        # Tradier sandbox returns {"order": {"id": 12345, "status": "ok"}}
        # "ok" means accepted, not filled — limit orders are never instantly filled.
        raw_id = order.get("id")
        if not raw_id:
            # Surface the full response so the caller can diagnose
            raise TradierError(
                f"Tradier did not return an order ID. Response: {resp!r}"
            )
        order_id = str(raw_id)
        status   = str(order.get("status", "ok"))

        logger.info(
            "[SANDBOX] Order accepted: id=%s status=%s",
            order_id, status,
        )

        return OrderResult(
            order_id     = order_id,
            status       = status,
            filled_price = preview.mid or 0.0,
            symbol       = preview.option_symbol,
            qty          = preview.contracts,
        )

    # ── Account / positions / orders ──────────────────────────────────────────

    def get_account(self) -> dict:
        """
        Fetch account balances from Tradier.

        Returns dict with keys: buying_power, cash, portfolio_value, equity.
        Requires TRADIER_ACCOUNT_ID env var.
        """
        account_id = os.environ.get("TRADIER_ACCOUNT_ID", "").strip()
        if not account_id:
            raise TradierError(
                "TRADIER_ACCOUNT_ID not set. "
                "Find your account ID at https://developer.tradier.com/user/profile"
            )
        import config as _cfg
        paper_bp = getattr(_cfg, "PAPER_BUYING_POWER", None)

        data     = self._get(f"/accounts/{account_id}/balances")
        balances = (data.get("balances") or {})
        live_bp  = float(balances.get("margin", {}).get("option_buying_power")
                         or balances.get("cash", {}).get("cash_available")
                         or balances.get("option_buying_power", 0) or 0)
        return {
            "buying_power":    paper_bp if paper_bp is not None else live_bp,
            "cash":            paper_bp if paper_bp is not None else float(balances.get("total_cash", 0) or 0),
            "portfolio_value": float(balances.get("total_equity", 0) or 0),
            "equity":          float(balances.get("total_equity", 0) or 0),
        }

    def get_positions(self) -> Dict[str, dict]:
        """
        Fetch open positions from Tradier.

        Returns dict keyed by symbol with qty, avg_entry, market_val, unrealized.
        Requires TRADIER_ACCOUNT_ID env var.
        """
        account_id = os.environ.get("TRADIER_ACCOUNT_ID", "").strip()
        if not account_id:
            raise TradierError("TRADIER_ACCOUNT_ID not set.")
        data      = self._get(f"/accounts/{account_id}/positions")
        positions = (data.get("positions") or {}).get("position") or []
        if isinstance(positions, dict):
            positions = [positions]
        result: Dict[str, dict] = {}
        for p in positions:
            sym = p.get("symbol", "")
            result[sym] = {
                "qty":        float(p.get("quantity", 0)),
                "avg_entry":  float(p.get("cost_basis", 0)) / max(float(p.get("quantity", 1)), 1),
                "market_val": float(p.get("cost_basis", 0)),  # Tradier doesn't return real-time mkt val here
                "unrealized": 0.0,  # not available in this endpoint
            }
        return result

    def get_open_orders(self) -> List[dict]:
        """
        Fetch open/pending orders from Tradier.

        Returns list of order dicts with keys: id, symbol, side, qty, status.
        Requires TRADIER_ACCOUNT_ID env var.
        """
        account_id = os.environ.get("TRADIER_ACCOUNT_ID", "").strip()
        if not account_id:
            raise TradierError("TRADIER_ACCOUNT_ID not set.")
        data   = self._get(f"/accounts/{account_id}/orders")
        orders = (data.get("orders") or {}).get("order") or []
        if isinstance(orders, dict):
            orders = [orders]
        return [
            {
                "id":     str(o.get("id", "")),
                "symbol": o.get("option_symbol") or o.get("symbol", ""),
                "side":   str(o.get("side", "")),
                "qty":    float(o.get("quantity", 0)),
                "status": str(o.get("status", "")),
            }
            for o in orders
        ]

    def buy_to_open_call(self, option_symbol: str, contracts: int = 1,
                         limit_price: Optional[float] = None) -> OrderResult:
        """Delegates to place_order() via a minimal preview-like object."""
        from types import SimpleNamespace
        preview = SimpleNamespace(
            underlying_symbol=option_symbol[:6].rstrip(),
            option_symbol=option_symbol,
            contracts=contracts,
            mid=limit_price,
        )
        return self.place_order(preview)

    def sell_to_close_call(self, option_symbol: str, contracts: int = 1,
                           limit_price: Optional[float] = None) -> OrderResult:
        """Submit a sell-to-close limit order via Tradier sandbox."""
        account_id = os.environ.get("TRADIER_ACCOUNT_ID", "").strip()
        if not account_id:
            raise TradierError("TRADIER_ACCOUNT_ID not set.")
        if not limit_price:
            raise TradierError("limit_price required for sell_to_close_call")

        payload = {
            "class":         "option",
            "symbol":        option_symbol[:6].rstrip(),
            "option_symbol": option_symbol,
            "side":          "sell_to_close",
            "quantity":      str(contracts),
            "type":          "limit",
            "duration":      "day",
            "price":         f"{limit_price:.2f}",
        }
        resp     = self._post(f"/accounts/{account_id}/orders", payload)
        order    = resp.get("order") or {}
        raw_id   = order.get("id")
        if not raw_id:
            raise TradierError(f"Tradier did not return an order ID. Response: {resp!r}")
        return OrderResult(
            order_id     = str(raw_id),
            status       = str(order.get("status", "ok")),
            filled_price = limit_price,
            symbol       = option_symbol,
            qty          = contracts,
        )
