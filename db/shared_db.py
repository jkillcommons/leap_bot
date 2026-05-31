import sqlite3
import os
from config import SHARED_DB_PATH


def _connect(readonly=True):
    if not os.path.exists(SHARED_DB_PATH):
        raise FileNotFoundError(f"Shared DB not found: {SHARED_DB_PATH}")
    if readonly:
        uri = f"file:{SHARED_DB_PATH}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(SHARED_DB_PATH)


def _row_to_dict(cursor, row):
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def get_leap_recommendations(n=20):
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM leap_recommendations ORDER BY run_date DESC LIMIT ?", (n,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except FileNotFoundError:
        return []


def get_watchlist(strategy=None, tier=None):
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM watchlist WHERE strategy IN ('leap','both') AND active=1"
        params = []
        if tier is not None:
            query += " AND tier=?"
            params.append(tier)
        query += " ORDER BY tier, symbol"
        cur = conn.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except FileNotFoundError:
        return []


def get_recommendation_by_ticker(ticker):
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM leap_recommendations WHERE ticker=? ORDER BY run_date DESC LIMIT 1",
            (ticker.upper(),),
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except FileNotFoundError:
        return None


def watchlist_add(symbol, strategy="leap", tier=2, added_by="telegram"):
    from datetime import date
    conn = _connect(readonly=False)
    conn.execute(
        """INSERT OR IGNORE INTO watchlist
           (symbol, strategy, tier, active, added_by, date_added)
           VALUES (?,?,?,1,?,?)""",
        (symbol.upper(), strategy, tier, added_by, date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def watchlist_remove(symbol):
    conn = _connect(readonly=False)
    conn.execute(
        "UPDATE watchlist SET active=0 WHERE symbol=? AND strategy IN ('leap','both')",
        (symbol.upper(),),
    )
    conn.commit()
    conn.close()


def watchlist_promote(symbol):
    conn = _connect(readonly=False)
    conn.execute(
        "UPDATE watchlist SET tier=1 WHERE symbol=? AND strategy IN ('leap','both') AND active=1",
        (symbol.upper(),),
    )
    conn.commit()
    conn.close()


def watchlist_exists(symbol):
    try:
        conn = _connect()
        cur = conn.execute(
            "SELECT 1 FROM watchlist WHERE symbol=? AND strategy IN ('leap','both') AND active=1",
            (symbol.upper(),),
        )
        found = cur.fetchone() is not None
        conn.close()
        return found
    except FileNotFoundError:
        return False


def get_put_candidates(min_score=65):
    """
    Return bearish candidates from the recommendations table.

    Bearish filter: wheel_score < 55 (proxy for bearish bias).
    The recommendations table has trend_label TEXT (e.g. 'bearish') but no
    numeric trend_score — a future research_bot update should add that column
    so this can be tightened to: trend_score >= min_score AND trend_label='bearish'.

    Returns rows ordered by most recent created_at, one per symbol (latest only).
    """
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT r.*
            FROM recommendations r
            INNER JOIN (
                SELECT symbol, MAX(created_at) AS latest
                FROM recommendations
                GROUP BY symbol
            ) latest ON r.symbol = latest.symbol AND r.created_at = latest.latest
            WHERE r.wheel_score < 55
            ORDER BY r.created_at DESC
            """,
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except (FileNotFoundError, sqlite3.OperationalError):
        return []


def get_last_screener_run():
    try:
        conn = _connect()
        cur = conn.execute(
            "SELECT MAX(run_date) as last_run FROM leap_recommendations"
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except FileNotFoundError:
        return None


def check_connection():
    try:
        conn = _connect()
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False
