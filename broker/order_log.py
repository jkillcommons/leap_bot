"""
broker/order_log.py — Append-only order log writers.

Two separate log files:
    logs/paper_orders.log    — /enter_paper fills
    logs/sandbox_orders.log  — /broker_preview confirmed Tradier sandbox orders

Format (TSV):
    timestamp | symbol | strike | expiration | fill_price | order_id | status | notes
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

_BASE = os.path.expanduser("~/leap_bot/logs")


def _write(filename: str, fields: list) -> None:
    os.makedirs(_BASE, exist_ok=True)
    path = os.path.join(_BASE, filename)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "\t".join([ts] + [str(f) for f in fields]) + "\n"
    with open(path, "a") as fh:
        fh.write(line)


def log_paper_order(
    symbol:      str,
    strike:      float,
    expiration:  str,
    fill_price:  float,
    order_id:    str,
    trade_db_id: int,
    notes:       str = "",
) -> None:
    """Append one row to paper_orders.log."""
    _write("paper_orders.log", [
        symbol, f"{strike:.2f}", expiration,
        f"{fill_price:.2f}", order_id, f"db_id={trade_db_id}",
        "paper_fill", notes,
    ])


def log_sandbox_order(
    symbol:     str,
    strike:     float,
    expiration: str,
    fill_price: float,
    order_id:   str,
    status:     str,
    notes:      str = "",
) -> None:
    """Append one row to sandbox_orders.log."""
    _write("sandbox_orders.log", [
        symbol, f"{strike:.2f}", expiration,
        f"{fill_price:.2f}", order_id, status,
        "sandbox", notes,
    ])
