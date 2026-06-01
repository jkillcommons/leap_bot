"""
risk_engine.py
==============
Shared portfolio risk engine. Enforces all 6 risk rules before
any trade entry. Used by all three bots.

Risk levels returned:
  GREEN  — all clear, proceed
  YELLOW — warning, proceed with reduced size
  RED    — blocked, do not enter

All methods return RiskResult dataclass. Never raises — returns
RED with reason on any error.
"""

import sqlite3
import os
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Account size — override via RISK_ACCOUNT_SIZE env var
def _default_account_size() -> float:
    val = os.getenv("RISK_ACCOUNT_SIZE", "")
    try:
        return float(val) if val else 100_000.0
    except ValueError:
        return 100_000.0

# Risk thresholds — calibrated for wheel strategy (2-3 concurrent CSPs normal)
# Override via env: HEAT_WARN_PCT, HEAT_BLOCK_PCT
def _pct(env_key, default):
    try:
        return float(os.getenv(env_key, "")) or default
    except ValueError:
        return default

HEAT_WARN_PCT        = _pct("HEAT_WARN_PCT",  0.20)
HEAT_BLOCK_PCT       = _pct("HEAT_BLOCK_PCT", 0.35)
DELTA_WARN           = 200
DELTA_BLOCK          = 300
VIX_CONDOR_STOP      = 28.0
VIX_CSP_STOP         = 30.0
VIX_CC_STOP          = 35.0
VIX_CRISIS           = 45.0
CIRCUIT_BREAKER_N    = 3
CIRCUIT_BREAKER_WINDOW = 5
CIRCUIT_BREAKER_PAUSE_HOURS = 48
DRAWDOWN_WARN_PCT    = -0.05
DRAWDOWN_STOP_PCT    = -0.08
SECTOR_MAX_POSITIONS = 2

VIX_THRESHOLDS = {
    'iron_condor':      VIX_CONDOR_STOP,
    'cash_secured_put': VIX_CSP_STOP,
    'covered_call':     VIX_CC_STOP,
    'long_call_leap':   None,
    'long_put':         None,
    'bull_call_spread': None,
    'diagonal_spread':  VIX_CC_STOP,
}

def calc_heat(play_type: str, params: dict) -> float:
    t = play_type
    if t == 'covered_call':
        return params.get('stock_price', 0) * params.get('shares', 100) * 0.20
    elif t == 'cash_secured_put':
        # 25% of notional — realistic max loss on a wheel CSP (stock drops 25% below strike)
        return params.get('strike', 0) * 100 * params.get('contracts', 1) * 0.25
    elif t in ('long_call_leap', 'long_put'):
        return params.get('premium', 0) * 100 * params.get('contracts', 1)
    elif t in ('bull_call_spread', 'diagonal_spread'):
        return params.get('net_debit', 0) * 100 * params.get('contracts', 1)
    elif t == 'iron_condor':
        return params.get('spread_width', 0) * 100 * params.get('contracts', 1)
    return 0.0


@dataclass
class RiskResult:
    level: str
    allowed: bool
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    heat_current: float = 0.0
    heat_proposed: float = 0.0
    heat_limit: float = 0.0
    portfolio_delta: float = 0.0
    mtd_pnl: float = 0.0
    vix_level: Optional[float] = None

    def summary(self) -> str:
        lines = [f"Risk: {self.level}"]
        if self.warnings:
            lines.append("Warnings: " + " | ".join(self.warnings))
        if self.reasons:
            lines.append("Blocked: " + " | ".join(self.reasons))
        lines.append(
            f"Heat: ${self.heat_current:,.0f} + ${self.heat_proposed:,.0f} "
            f"proposed / ${self.heat_limit:,.0f} limit"
        )
        lines.append(f"Portfolio delta: {self.portfolio_delta:+.0f}")
        lines.append(f"MTD P&L: ${self.mtd_pnl:+,.0f}")
        return "\n".join(lines)


class RiskEngine:
    def __init__(
        self,
        account_size: Optional[float] = None,
        shared_db_path: str = "",
        wheel_db_path: Optional[str] = None,
        leaps_db_path: Optional[str] = None,
    ):
        self.account_size = account_size if account_size is not None else _default_account_size()
        self.shared_db = shared_db_path
        self.wheel_db = wheel_db_path
        self.leaps_db = leaps_db_path
        self._circuit_breaker_file = Path.home() / ".zulucare_circuit_breaker"

    def check_entry(self, play_type, symbol, sector, heat_params, vix=None):
        result = RiskResult(
            level='GREEN',
            allowed=True,
            heat_limit=self.account_size * HEAT_BLOCK_PCT,
        )
        try:
            cb = self._check_circuit_breaker()
            if cb:
                result.level = 'RED'
                result.allowed = False
                result.reasons.append(cb)
                return result

            if vix is not None:
                result.vix_level = vix
                vix_limit = VIX_THRESHOLDS.get(play_type)
                if vix >= VIX_CRISIS:
                    result.level = 'RED'
                    result.allowed = False
                    result.reasons.append(f"VIX crisis level {vix:.1f} — all short premium suspended")
                elif vix_limit and vix >= vix_limit:
                    result.level = 'RED'
                    result.allowed = False
                    result.reasons.append(f"VIX {vix:.1f} ≥ {vix_limit} threshold for {play_type}")

            current_heat = self._compute_current_heat()
            proposed_heat = calc_heat(play_type, heat_params)
            total_heat = current_heat + proposed_heat
            heat_warn  = self.account_size * HEAT_WARN_PCT
            heat_block = self.account_size * HEAT_BLOCK_PCT
            result.heat_current  = current_heat
            result.heat_proposed = proposed_heat

            if total_heat >= heat_block:
                result.level = 'RED'
                result.allowed = False
                result.reasons.append(
                    f"Portfolio heat {total_heat/self.account_size*100:.1f}% ≥ {HEAT_BLOCK_PCT*100:.0f}% limit"
                )
            elif total_heat >= heat_warn:
                if result.level == 'GREEN':
                    result.level = 'YELLOW'
                result.warnings.append(
                    f"Heat at {total_heat/self.account_size*100:.1f}% (warn at {HEAT_WARN_PCT*100:.0f}%)"
                )

            delta = self._compute_portfolio_delta()
            result.portfolio_delta = delta
            proposed_delta = heat_params.get('delta', 0)
            total_delta = delta + proposed_delta

            if abs(total_delta) >= DELTA_BLOCK:
                result.level = 'RED'
                result.allowed = False
                result.reasons.append(f"Portfolio delta {total_delta:+.0f} ≥ ±{DELTA_BLOCK} limit")
            elif abs(total_delta) >= DELTA_WARN:
                if result.level == 'GREEN':
                    result.level = 'YELLOW'
                result.warnings.append(f"Delta {total_delta:+.0f} approaching ±{DELTA_BLOCK} limit")

            mtd = self._compute_mtd_pnl()
            result.mtd_pnl = mtd
            mtd_pct = mtd / self.account_size if self.account_size > 0 else 0

            if mtd_pct <= DRAWDOWN_STOP_PCT:
                result.level = 'RED'
                result.allowed = False
                result.reasons.append(f"MTD drawdown {mtd_pct*100:.1f}% ≤ {DRAWDOWN_STOP_PCT*100:.0f}% limit")
            elif mtd_pct <= DRAWDOWN_WARN_PCT:
                if result.level == 'GREEN':
                    result.level = 'YELLOW'
                result.warnings.append(f"MTD drawdown {mtd_pct*100:.1f}% — reduce position size")

            if sector:
                sector_count = self._count_sector_positions(sector)
                if sector_count >= SECTOR_MAX_POSITIONS:
                    result.level = 'RED'
                    result.allowed = False
                    result.reasons.append(
                        f"Sector '{sector}' already has {sector_count} positions (max {SECTOR_MAX_POSITIONS})"
                    )

        except Exception as e:
            logger.error("Risk engine error: %s", e)
            result.level = 'RED'
            result.allowed = False
            result.reasons.append(f"Risk engine error: {e}")

        return result

    def resume_circuit_breaker(self) -> str:
        if self._circuit_breaker_file.exists():
            self._circuit_breaker_file.unlink()
            return "Circuit breaker cleared. Trading resumed."
        return "No active circuit breaker."

    def get_portfolio_snapshot(self) -> dict:
        heat  = self._compute_current_heat()
        delta = self._compute_portfolio_delta()
        mtd   = self._compute_mtd_pnl()
        cb    = self._check_circuit_breaker()
        consecutive = self._count_consecutive_losses()

        heat_pct = heat / self.account_size if self.account_size > 0 else 0
        mtd_pct  = mtd  / self.account_size if self.account_size > 0 else 0

        return {
            'account_size':       self.account_size,
            'heat_dollars':       heat,
            'heat_pct':           heat_pct,
            'heat_status':        'RED' if heat_pct >= HEAT_BLOCK_PCT else 'YELLOW' if heat_pct >= HEAT_WARN_PCT else 'GREEN',
            'portfolio_delta':    delta,
            'delta_status':       'RED' if abs(delta) >= DELTA_BLOCK else 'YELLOW' if abs(delta) >= DELTA_WARN else 'GREEN',
            'mtd_pnl':            mtd,
            'mtd_pct':            mtd_pct,
            'drawdown_status':    'RED' if mtd_pct <= DRAWDOWN_STOP_PCT else 'YELLOW' if mtd_pct <= DRAWDOWN_WARN_PCT else 'GREEN',
            'circuit_breaker':    cb,
            'cb_status':          'RED' if cb else 'GREEN',
            'consecutive_losses': consecutive,
            'vix_level':          None,  # populated externally when VIX is fetched
            'thresholds': {
                'heat_warn':      HEAT_WARN_PCT,
                'heat_block':     HEAT_BLOCK_PCT,
                'delta_warn':     DELTA_WARN,
                'delta_block':    DELTA_BLOCK,
                'drawdown_warn':  DRAWDOWN_WARN_PCT,
                'drawdown_stop':  DRAWDOWN_STOP_PCT,
                'vix_thresholds': VIX_THRESHOLDS,
            }
        }

    # Private helpers

    def _q(self, db_path, sql, params=()):
        if not db_path or not os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(sql, params)]
            conn.close()
            return rows
        except Exception as e:
            logger.warning("DB query error (%s): %s", db_path, e)
            return []

    def _compute_current_heat(self) -> float:
        total = 0.0
        if self.wheel_db:
            # JOIN positions with open_options — strike/contracts are in open_options
            rows = self._q(self.wheel_db, """
                SELECT p.state, p.cost_basis, p.shares_held,
                       o.strike, o.contracts
                FROM positions p
                LEFT JOIN open_options o ON p.symbol = o.symbol
                WHERE p.state NOT IN ('FLAT','CALLED_AWAY')
            """)
            for p in rows:
                state = p.get('state', '')
                if state in ('SHORT_PUT',):
                    total += (p.get('strike') or 0) * 100 * (p.get('contracts') or 1)
                else:
                    total += (p.get('cost_basis') or 0) * (p.get('shares_held') or 100) * 0.20
        if self.leaps_db:
            trades = self._q(self.leaps_db,
                "SELECT entry_price, contracts FROM paper_trades WHERE status='open'")
            for t in trades:
                total += (t.get('entry_price') or 0) * 100 * (t.get('contracts') or 1)
        return total

    def _compute_portfolio_delta(self) -> float:
        total = 0.0
        if self.wheel_db:
            rows = self._q(self.wheel_db, """
                SELECT p.state, p.shares_held, o.contracts
                FROM positions p
                LEFT JOIN open_options o ON p.symbol = o.symbol
                WHERE p.state NOT IN ('FLAT','CALLED_AWAY')
            """)
            for p in rows:
                shares = p.get('shares_held') or 0
                if shares > 0:
                    total += shares
                state = p.get('state', '')
                if state in ('SHORT_CALL',):
                    contracts = p.get('contracts') or 1
                    total -= 0.30 * 100 * contracts  # use 0.30 default (no delta stored)
        if self.leaps_db:
            trades = self._q(self.leaps_db,
                "SELECT delta_at_entry, contracts, play_type FROM paper_trades WHERE status='open'")
            for t in trades:
                delta     = t.get('delta_at_entry') or 0.80
                contracts = t.get('contracts') or 1
                play      = t.get('play_type') or 'long_call_leap'
                sign = -1 if play == 'long_put' else +1
                total += sign * abs(delta) * 100 * contracts
        return total

    def _compute_mtd_pnl(self) -> float:
        first_of_month = date.today().replace(day=1).isoformat()
        rows = self._q(self.shared_db,
            "SELECT SUM(pnl_dollars) as total FROM trade_journal "
            "WHERE exit_date >= ? AND win IS NOT NULL",
            (first_of_month,))
        if rows and rows[0].get('total') is not None:
            return float(rows[0]['total'])
        return 0.0

    def _check_circuit_breaker(self) -> Optional[str]:
        if self._circuit_breaker_file.exists():
            try:
                content = self._circuit_breaker_file.read_text().strip()
                pause_until = datetime.fromisoformat(content)
                if datetime.now() < pause_until:
                    remaining = pause_until - datetime.now()
                    hours = int(remaining.total_seconds() / 3600)
                    return f"Circuit breaker active — {hours}h remaining. /resume to clear."
                else:
                    self._circuit_breaker_file.unlink()
            except Exception:
                pass
        consecutive = self._count_consecutive_losses()
        if consecutive >= CIRCUIT_BREAKER_N:
            pause_until = datetime.now() + timedelta(hours=CIRCUIT_BREAKER_PAUSE_HOURS)
            try:
                self._circuit_breaker_file.write_text(pause_until.isoformat())
            except Exception:
                pass
            return (f"{consecutive} consecutive losses — "
                    f"48h pause auto-triggered. /resume to override.")
        return None

    def _count_consecutive_losses(self) -> int:
        rows = self._q(self.shared_db,
            f"SELECT win FROM trade_journal WHERE win IS NOT NULL "
            f"ORDER BY exit_date DESC, id DESC LIMIT {CIRCUIT_BREAKER_WINDOW}")
        count = 0
        for r in rows:
            if r['win'] == 0:
                count += 1
            else:
                break
        return count

    def _count_sector_positions(self, sector: str) -> int:
        rows = self._q(self.shared_db,
            "SELECT ticker FROM watchlist WHERE sector = ?", (sector,))
        tickers_in_sector = {r['ticker'] for r in rows}
        count = 0
        if self.wheel_db:
            for row in self._q(self.wheel_db,
                "SELECT symbol FROM positions WHERE state NOT IN ('FLAT','CALLED_AWAY')"):
                if row['symbol'] in tickers_in_sector:
                    count += 1
        if self.leaps_db:
            for row in self._q(self.leaps_db,
                "SELECT ticker FROM paper_trades WHERE status='open'"):
                if row['ticker'] in tickers_in_sector:
                    count += 1
        return count
