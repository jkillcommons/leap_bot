"""
position_monitor.py — check open LEAP/put positions for stop/target/DTE alerts.
"""
from __future__ import annotations

from datetime import date
from typing import Optional


# ── thresholds ────────────────────────────────────────────────────────────────

CALL_TARGET_GAIN  =  1.00   # +100%
CALL_STOP_LOSS    = -0.50   # -50%
PUT_TARGET_GAIN   =  0.75   # +75%
PUT_STOP_LOSS     = -0.40   # -40%
DTE_ALERT_DAYS    = 14
DTE_FORCE_CLOSE   = 7


class PositionMonitor:
    def __init__(self, leap_db, dual_broker, telegram_send_fn):
        """
        leap_db         — db.leap_db module (already imported by caller)
        dual_broker     — broker instance with get_option_chain()
        telegram_send_fn — callable(str) → None
        """
        self.db     = leap_db
        self.broker = dual_broker
        self.send   = telegram_send_fn

    # ── public ────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Check all open positions.  Sends Telegram alerts for stops/targets/DTE.
        Returns {"checked": N, "alerts": M}.
        """
        positions = self.db.get_all_open()
        alerts = 0

        for trade in positions:
            try:
                fired = self._check_position(trade)
                if fired:
                    alerts += 1
            except Exception:
                pass  # never crash the whole run for a single position

        return {"checked": len(positions), "alerts": alerts}

    # ── private ───────────────────────────────────────────────────────────────

    def _check_position(self, trade: dict) -> bool:
        """Return True if any alert was sent for this position."""
        ticker     = trade["ticker"]
        strike     = trade["strike"]
        expiration = trade["expiration"]
        entry      = trade.get("entry_price") or 0
        play_type  = trade.get("play_type") or "long_call_leap"
        option_type = "put" if play_type == "long_put" else "call"

        dte = self._compute_dte(expiration)
        current = self._get_option_price(ticker, strike, expiration, option_type)

        fired = False

        # DTE alerts (check even without live price)
        if dte is not None:
            if dte <= DTE_FORCE_CLOSE:
                msg = (
                    f"⚠️ <b>DTE FORCE CLOSE</b>\n"
                    f"{ticker} ${strike:.0f}{option_type[0].upper()}  Exp: {expiration}\n"
                    f"Only {dte} DTE remaining — consider closing now."
                )
                self.send(msg)
                fired = True
            elif dte <= DTE_ALERT_DAYS:
                msg = (
                    f"⏰ <b>DTE ALERT</b>\n"
                    f"{ticker} ${strike:.0f}{option_type[0].upper()}  Exp: {expiration}\n"
                    f"{dte} DTE remaining."
                )
                self.send(msg)
                fired = True

        if current is None or entry == 0:
            return fired

        gain = (current - entry) / entry

        # Determine thresholds based on play_type
        if play_type == "long_put":
            target_thresh = PUT_TARGET_GAIN
            stop_thresh   = PUT_STOP_LOSS
        else:
            target_thresh = CALL_TARGET_GAIN
            stop_thresh   = CALL_STOP_LOSS

        pnl_per_contract = (current - entry) * 100
        sign = "+" if pnl_per_contract >= 0 else ""

        if gain >= target_thresh:
            msg = (
                f"🎯 <b>PROFIT TARGET HIT</b>\n"
                f"{ticker} ${strike:.0f}{option_type[0].upper()}  Exp: {expiration}\n"
                f"Entry: ${entry:.2f}  Now: ${current:.2f}\n"
                f"Gain: {gain:+.0%}  P&L: {sign}${pnl_per_contract:.2f}/contract\n"
                f"Consider closing."
            )
            self.send(msg)
            fired = True
        elif gain <= stop_thresh:
            msg = (
                f"🛑 <b>STOP LOSS HIT</b>\n"
                f"{ticker} ${strike:.0f}{option_type[0].upper()}  Exp: {expiration}\n"
                f"Entry: ${entry:.2f}  Now: ${current:.2f}\n"
                f"Loss: {gain:+.0%}  P&L: {sign}${pnl_per_contract:.2f}/contract\n"
                f"Consider closing."
            )
            self.send(msg)
            fired = True

        return fired

    def _get_option_price(self, symbol: str, strike: float, expiry: str,
                          option_type: str) -> Optional[float]:
        """Fetch the mid price for the given contract from the broker."""
        try:
            import config
            max_dte = 800
            chain = self.broker.get_option_chain(
                symbol, option_type,
                min_dte=0,
                max_dte=max_dte,
            )
            if not chain:
                return None
            chain_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chain]
            match = next(
                (c for c in chain_dicts
                 if abs(c.get("strike", 0) - strike) < 0.01
                 and str(c.get("expiration_date", "")).startswith(expiry[:7])),
                None,
            )
            if not match:
                return None
            mid = match.get("mid")
            return float(mid) if mid is not None else None
        except Exception:
            return None

    def _compute_dte(self, expiry_str: str) -> Optional[int]:
        """Return calendar days to expiry or None if unparseable."""
        try:
            exp_date = date.fromisoformat(expiry_str[:10])
            return (exp_date - date.today()).days
        except (ValueError, TypeError):
            return None
