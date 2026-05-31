import sqlite3
import os
from datetime import date
from config import LEAP_DB_PATH

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    entered_date TEXT NOT NULL,
    strike       REAL NOT NULL,
    expiration   TEXT NOT NULL,
    contracts    INTEGER DEFAULT 1,
    entry_price  REAL NOT NULL,
    current_price REAL,
    breakeven    REAL,
    target_exit  REAL,
    status       TEXT DEFAULT 'open',
    exit_price   REAL,
    exit_date    TEXT,
    pnl          REAL,
    notes        TEXT,
    journal_id   INTEGER,    -- trade_journal.id in wheel_research.db
    play_type    TEXT DEFAULT 'long_call_leap'
);
"""


def _load_jdb():
    """Import journal_db from research_bot, return module or None."""
    import sys as _sys
    _rb = os.path.expanduser("~/research_bot")
    if _rb not in _sys.path:
        _sys.path.insert(0, _rb)
    try:
        import journal_db as _jdb
        return _jdb
    except ImportError:
        return None


def _connect():
    os.makedirs(os.path.dirname(LEAP_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(LEAP_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_SQL)
    # Migrations: add columns to existing DBs that pre-date them
    for migration in (
        "ALTER TABLE paper_trades ADD COLUMN journal_id INTEGER",
        "ALTER TABLE paper_trades ADD COLUMN play_type TEXT DEFAULT 'long_call_leap'",
    ):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def add_paper_trade(ticker, strike, expiration, entry_price,
                    breakeven, target_exit=None, notes="", contracts=1,
                    play_type="long_call_leap"):
    conn = _connect()
    today_str = date.today().isoformat()
    cur = conn.execute(
        """INSERT INTO paper_trades
           (ticker, entered_date, strike, expiration, contracts,
            entry_price, breakeven, target_exit, notes, status, play_type)
           VALUES (?,?,?,?,?,?,?,?,?,'open',?)""",
        (ticker.upper(), today_str, float(strike),
         expiration, contracts, float(entry_price),
         float(breakeven), float(target_exit) if target_exit else None,
         notes, play_type),
    )
    trade_id = cur.lastrowid
    conn.commit()

    # Log to shared trade journal (non-fatal if unavailable)
    try:
        jdb = _load_jdb()
        if jdb is not None:
            from datetime import date as _date, datetime as _dt
            try:
                exp_date = _date.fromisoformat(expiration)
                dte = (exp_date - _date.today()).days
            except (ValueError, TypeError):
                dte = None
            premium_paid = float(entry_price) * 100 * int(contracts)
            journal_id = jdb.log_trade(
                bot_source="leaps_bot",
                play_type=play_type,
                symbol=ticker.upper(),
                entry_date=today_str,
                dte_at_entry=dte,
                strike_bought=float(strike),
                expiry=expiration,
                contracts=int(contracts),
                premium_paid=premium_paid,
                notes=notes or "",
            )
            conn.execute(
                "UPDATE paper_trades SET journal_id = ? WHERE id = ?",
                (journal_id, trade_id),
            )
            conn.commit()
    except Exception:
        pass  # journal failure must not break trade entry

    conn.close()
    return trade_id


def update_price(ticker, current_price):
    conn = _connect()
    conn.execute(
        "UPDATE paper_trades SET current_price=? WHERE ticker=? AND status='open'",
        (float(current_price), ticker.upper()),
    )
    conn.commit()
    conn.close()


def close_trade(ticker, exit_price, notes=""):
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE ticker=? AND status='open' ORDER BY id DESC LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    pnl = (float(exit_price) - row["entry_price"]) * 100 * row["contracts"]
    today_str = date.today().isoformat()
    conn.execute(
        """UPDATE paper_trades
           SET status='closed', exit_price=?, exit_date=?, pnl=?, notes=?
           WHERE id=?""",
        (float(exit_price), today_str, pnl,
         notes or row["notes"], row["id"]),
    )
    conn.commit()
    conn.close()

    # Close the journal trade if one was logged at entry (non-fatal if unavailable)
    journal_id = row["journal_id"] if "journal_id" in row.keys() else None
    if journal_id is not None:
        try:
            jdb = _load_jdb()
            if jdb is not None:
                cost_basis = row["entry_price"] * 100 * row["contracts"]
                pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0
                jdb.close_trade(
                    trade_id=journal_id,
                    exit_date=today_str,
                    exit_price=float(exit_price),
                    pnl_dollars=pnl,
                    pnl_pct=round(pnl_pct, 2),
                    exit_reason="manual",
                    notes=notes or "",
                )
        except Exception:
            pass  # journal failure must not break trade close

    return {"pnl": pnl, "entry_price": row["entry_price"],
            "contracts": row["contracts"], "id": row["id"]}


def get_open_trades():
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' ORDER BY entered_date DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_open_puts():
    """Return open long-put paper positions only."""
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' AND play_type='long_put' ORDER BY entered_date DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_all_open():
    """Return all open positions regardless of play_type."""
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE status='open' ORDER BY entered_date DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_closed_trades():
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE status='closed' ORDER BY exit_date DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_trade_by_ticker(ticker):
    conn = _connect()
    cur = conn.execute(
        "SELECT * FROM paper_trades WHERE ticker=? AND status='open' ORDER BY id DESC LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_trade_summary():
    open_trades  = get_open_trades()
    closed_trades = get_closed_trades()

    if not closed_trades:
        return {
            "total_open": len(open_trades),
            "total_closed": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "avg_pnl_per_trade": 0.0,
            "best_trade": None,
            "worst_trade": None,
        }

    pnls = [t["pnl"] for t in closed_trades if t["pnl"] is not None]
    winners = [p for p in pnls if p > 0]
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0

    return {
        "total_open": len(open_trades),
        "total_closed": len(closed_trades),
        "total_pnl": sum(pnls),
        "win_rate": round(len(winners) / len(pnls) * 100, 1) if pnls else 0.0,
        "avg_pnl_per_trade": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "best_trade": best,
        "worst_trade": worst,
    }


def check_connection():
    try:
        conn = _connect()
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False
