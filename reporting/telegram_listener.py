import time
import logging
import os
import requests
from datetime import datetime, timedelta

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from reporting.telegram_alerts import send_message
import db.shared_db as sdb
import db.leap_db as ldb

log = logging.getLogger(__name__)

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
PENDING_TTL = 60  # seconds

_pending = {}  # chat_id → {"action": ..., "expires": datetime}


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt(val, prefix="", suffix="", decimals=2):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{prefix}{val:.{decimals}f}{suffix}"
    return f"{prefix}{val}{suffix}"


def _pending_set(chat_id, action):
    _pending[chat_id] = {"action": action, "expires": datetime.utcnow() + timedelta(seconds=PENDING_TTL)}


def _pending_get(chat_id):
    p = _pending.get(chat_id)
    if p and datetime.utcnow() < p["expires"]:
        return p["action"]
    _pending.pop(chat_id, None)
    return None


def _pending_clear(chat_id):
    _pending.pop(chat_id, None)


# ── command handlers ──────────────────────────────────────────────────────────

def cmd_help(args, chat_id):
    send_message(
        "🚀 <b>LEAP Bot — Commands</b>\n\n"
        "📋 <b>Recommendations</b>\n"
        "/leaps          — latest LEAP candidates from screener\n"
        "/leaps SYMBOL   — most recent rec for specific ticker\n"
        "/chain SYMBOL   — live option chain, best LEAP candidate\n\n"
        "👁 <b>Watchlist</b>\n"
        "/watchlist                     — view LEAP watchlist (Tier 1 and Tier 2)\n"
        "/watchlist promote SYMBOL — promote Tier 2 → Tier 1 (requires /confirm)\n"
        "/watchlist add SYMBOL     — manually add to Tier 2\n"
        "/watchlist remove SYMBOL  — soft-delete from watchlist\n\n"
        "📝 <b>Paper Trading</b>\n"
        "/leap_preview SYM   — screener rec + live pricing preview\n"
        "/enter_paper SYM    — log paper trade from screener rec\n"
        "/broker_preview SYM — sandbox preview + /confirm to submit\n"
        "/enter SYMBOL STRIKE EXP PRICE  — manual paper entry\n"
        "  example: /enter QQQ 480 2027-01-15 18.50\n"
        "/close SYMBOL PRICE [notes]      — close a paper trade\n"
        "/update SYMBOL PRICE             — update current price\n"
        "/trades                          — open paper positions\n"
        "/pnl                             — closed trade P&amp;L summary\n\n"
        "⚙️ <b>System</b>\n"
        "/status         — bot health, DB connection, broker mode\n"
        "/confirm        — confirm a pending action\n\n"
        "🐻 <b>Long Puts (Bearish)</b>\n"
        "/puts           — list open put positions with DTE and gain\n"
        "/put_scan       — scan screener for bearish candidates, show best put\n\n"
        "📒 <b>Trade Journal</b>\n"
        "/journal [N]    — last N closed trades (default 10)\n"
        "/scorecard      — win rates &amp; P&amp;L by play type; promotion status\n\n"
        "📡 <b>Monitoring</b>\n"
        "/leaps_status   — open positions, capital deployed, P&amp;L snapshot\n"
        "/monitor        — check all positions for stop/target/DTE alerts",
        chat_id=chat_id,
    )


def _format_rec(r):
    lines = [
        f"🚀 <b>{r.get('ticker','—')}</b>  Score: {_fmt(r.get('leap_score'))}  Delta: {_fmt(r.get('suggested_delta'))}",
        f"Strike: {_fmt(r.get('strike'))}  Exp: {r.get('exp_range') or '—'}",
        f"Ask: {_fmt(r.get('ask_price'),'$')}  Mid: {_fmt(r.get('mid_price'),'$')}  Breakeven: {_fmt(r.get('breakeven'),'$')}",
        f"IV: {_fmt(r.get('iv_level'))}%  Trend: {_fmt(r.get('trend_score'))}  Risk: {r.get('risk_rating') or '—'}",
        f"Rev growth: {_fmt(r.get('revenue_growth'))}%  EPS: {_fmt(r.get('eps_growth'))}%",
        f"Exit: {r.get('exit_thesis') or '—'}",
        f"Status: {r.get('status') or '—'}  Run: {r.get('run_date') or '—'}",
    ]
    return "\n".join(lines)


def cmd_leaps(args, chat_id):
    if args:
        ticker = args[0].upper()
        rec = sdb.get_recommendation_by_ticker(ticker)
        if not rec:
            send_message(f"No LEAP recommendation found for {ticker}.", chat_id=chat_id)
            return
        send_message(_format_rec(rec), chat_id=chat_id)
        return

    recs = sdb.get_leap_recommendations(20)
    if not recs:
        send_message("📋 No LEAP recommendations in screener DB yet.", chat_id=chat_id)
        return

    chunks = [recs[i:i+5] for i in range(0, len(recs), 5)]
    for chunk in chunks:
        send_message("\n\n".join(_format_rec(r) for r in chunk), chat_id=chat_id)


def cmd_watchlist(args, chat_id):
    if args:
        sub = args[0].lower()
        if sub == "promote" and len(args) >= 2:
            symbol = args[1].upper()
            if not sdb.watchlist_exists(symbol):
                send_message(f"❌ {symbol} not found in LEAP watchlist.", chat_id=chat_id)
                return
            _pending_set(chat_id, {"type": "promote", "symbol": symbol})
            send_message(
                f"Promote <b>{symbol}</b> to Tier 1? Reply /confirm within 60s",
                chat_id=chat_id,
            )
            return

        if sub == "add" and len(args) >= 2:
            symbol = args[1].upper()
            sdb.watchlist_add(symbol)
            send_message(f"✅ {symbol} added to LEAP watchlist (Tier 2)", chat_id=chat_id)
            return

        if sub == "remove" and len(args) >= 2:
            symbol = args[1].upper()
            sdb.watchlist_remove(symbol)
            send_message(f"⬛ {symbol} removed from LEAP watchlist", chat_id=chat_id)
            return

        send_message("Unknown /watchlist sub-command. See /help.", chat_id=chat_id)
        return

    rows = sdb.get_watchlist()
    if not rows:
        send_message("👁 LEAP watchlist is empty.", chat_id=chat_id)
        return

    tier1 = [r for r in rows if r.get("tier") == 1]
    tier2 = [r for r in rows if r.get("tier") != 1]

    lines = ["👁 <b>LEAP Watchlist</b>\n"]
    lines.append("<b>Tier 1 — Active</b>")
    if tier1:
        for r in tier1:
            lines.append(f"  {r['symbol']}  added {r.get('date_added','—')}  by {r.get('added_by','—')}")
    else:
        lines.append("  (none)")

    lines.append("\n<b>Tier 2 — Monitor</b>")
    if tier2:
        for r in tier2:
            lines.append(f"  {r['symbol']}  added {r.get('date_added','—')}  by {r.get('added_by','—')}")
    else:
        lines.append("  (none)")

    send_message("\n".join(lines), chat_id=chat_id)


def cmd_confirm(args, chat_id):
    action = _pending_get(chat_id)
    if not action:
        send_message("No pending action (or it expired). Try the command again.", chat_id=chat_id)
        return
    _pending_clear(chat_id)

    if action["type"] == "promote":
        symbol = action["symbol"]
        sdb.watchlist_promote(symbol)
        send_message(f"✅ {symbol} promoted to Tier 1", chat_id=chat_id)

    elif action["type"] == "sandbox_order":
        _execute_sandbox_order(action, chat_id)


def cmd_enter(args, chat_id):
    if len(args) < 4:
        send_message(
            "Usage: /enter SYMBOL STRIKE EXP PRICE [DELTA] [IV]\n"
            "Example: /enter AAPL 180 2027-01-15 12.50 0.82 0.28",
            chat_id=chat_id,
        )
        return
    try:
        ticker      = args[0].upper()
        strike      = float(args[1])
        expiration  = args[2]
        entry_price = float(args[3])
        delta       = float(args[4]) if len(args) > 4 else None
        iv_rank_val = float(args[5]) if len(args) > 5 else None
        datetime.strptime(expiration, "%Y-%m-%d")
    except ValueError as e:
        send_message(f"❌ Invalid input: {e}", chat_id=chat_id)
        return

    from datetime import date as _date
    try:
        dte_days = (_date.fromisoformat(expiration) - _date.today()).days
    except (ValueError, TypeError):
        dte_days = None

    breakeven       = strike + entry_price
    stop_loss_price = round(entry_price * 0.50, 2)
    target_price    = round(entry_price * 2.00, 2)

    trade_id = ldb.add_paper_trade(
        ticker, strike, expiration, entry_price, breakeven,
        target_exit=target_price,
        stop_loss_price=stop_loss_price,
        delta_at_entry=delta,
        iv_rank_at_entry=iv_rank_val,
    )
    journal_id = ldb.get_journal_id(trade_id)

    delta_str = f"{delta:.2f}" if delta is not None else "—"
    iv_str    = f"{iv_rank_val:.2f}" if iv_rank_val is not None else "—"
    jid_line  = f"Journal ID: {journal_id}\n" if journal_id else ""

    send_message(
        f"📝 <b>LEAP ENTERED</b>\n"
        f"{ticker}  {strike:.0f}C  Exp: {expiration}\n"
        f"Entry: ${entry_price}  Delta: {delta_str}  IV: {iv_str}\n"
        f"Stop loss: ${stop_loss_price:.2f}  (50% loss)\n"
        f"Target:    ${target_price:.2f}  (100% gain)\n"
        f"Breakeven: ${breakeven:.2f}\n"
        f"Trade ID: {trade_id}\n"
        f"{jid_line}"
        f"\nUpdate price: /update {ticker} [price]\n"
        f"Close: /close {ticker} [price] [reason]",
        chat_id=chat_id,
    )


def cmd_close(args, chat_id):
    if len(args) < 2:
        send_message("Usage: /close SYMBOL PRICE [notes]", chat_id=chat_id)
        return
    try:
        ticker = args[0].upper()
        exit_price = float(args[1])
        notes = " ".join(args[2:]) if len(args) > 2 else ""
    except ValueError as e:
        send_message(f"❌ Invalid input: {e}", chat_id=chat_id)
        return

    # Peek at the open trade to compute exit_reason before closing
    open_trade = ldb.get_trade_by_ticker(ticker)
    if open_trade and open_trade.get("entry_price"):
        _pnl_pct = ((exit_price - open_trade["entry_price"]) / open_trade["entry_price"]) * 100
        if _pnl_pct >= 90:
            exit_reason = "profit_target"
        elif _pnl_pct <= -45:
            exit_reason = "stop_loss"
        else:
            exit_reason = "manual"
    else:
        exit_reason = "manual"

    result = ldb.close_trade(ticker, exit_price, notes, exit_reason=exit_reason)
    if not result:
        send_message(f"❌ No open trade found for {ticker}.", chat_id=chat_id)
        return

    pnl = result["pnl"]
    pnl_pct = ((exit_price - result["entry_price"]) / result["entry_price"]) * 100
    sign = "+" if pnl >= 0 else ""
    send_message(
        f"✅ <b>TRADE CLOSED</b>\n"
        f"{ticker}  Exit: ${exit_price}\n"
        f"P&amp;L: {sign}${pnl:.2f}  ({sign}{pnl_pct:.1f}%)\n"
        f"Reason: {exit_reason}\n"
        + (f"{notes}" if notes else ""),
        chat_id=chat_id,
    )


def cmd_update(args, chat_id):
    if len(args) < 2:
        send_message("Usage: /update SYMBOL PRICE", chat_id=chat_id)
        return
    try:
        ticker = args[0].upper()
        price = float(args[1])
    except ValueError as e:
        send_message(f"❌ Invalid input: {e}", chat_id=chat_id)
        return

    trade = ldb.get_trade_by_ticker(ticker)
    if not trade:
        send_message(f"❌ No open trade found for {ticker}.", chat_id=chat_id)
        return

    ldb.update_price(ticker, price)
    unrealized = (price - trade["entry_price"]) * 100 * trade["contracts"]
    pct = ((price - trade["entry_price"]) / trade["entry_price"]) * 100
    sign = "+" if unrealized >= 0 else ""
    send_message(
        f"📊 <b>{ticker}</b> updated to ${price}\n"
        f"Entry: ${trade['entry_price']}  Unrealized: {sign}${unrealized:.2f} ({sign}{pct:.1f}%)",
        chat_id=chat_id,
    )


def cmd_trades(args, chat_id):
    rows = ldb.get_open_trades()
    if not rows:
        send_message("📋 No open LEAP positions.", chat_id=chat_id)
        return

    lines = ["📋 <b>Open LEAP Positions</b>\n"]
    for t in rows:
        cur = t.get("current_price")
        unr = None
        if cur is not None:
            unr = (cur - t["entry_price"]) * 100 * t["contracts"]
        lines.append(
            f"<b>{t['ticker']}</b>  {t['strike']}C {t['expiration']}\n"
            f"Entry: ${t['entry_price']}  Current: {_fmt(cur,'$')}\n"
            f"Unrealized: {_fmt(unr,'$')}  Entered: {t['entered_date']}"
        )
    send_message("\n\n".join(lines), chat_id=chat_id)


def cmd_pnl(args, chat_id):
    s = ldb.get_trade_summary()
    if s["total_closed"] == 0:
        send_message("📊 No closed trades yet.", chat_id=chat_id)
        return
    send_message(
        f"📊 <b>LEAP P&amp;L Summary</b>\n"
        f"Open: {s['total_open']}  Closed: {s['total_closed']}\n"
        f"Total P&amp;L: ${s['total_pnl']:.2f}\n"
        f"Win rate: {s['win_rate']}%\n"
        f"Avg per trade: ${s['avg_pnl_per_trade']:.2f}\n"
        f"Best: ${s['best_trade']:.2f}  Worst: ${s['worst_trade']:.2f}",
        chat_id=chat_id,
    )


def cmd_status(args, chat_id):
    import config
    shared_ok  = sdb.check_connection()
    leap_ok    = ldb.check_connection()
    open_count = len(ldb.get_open_trades())
    last_run   = sdb.get_last_screener_run()

    # Broker health check
    broker_line = "—"
    try:
        from broker.factory import make_broker
        broker = make_broker()
        acct   = broker.get_account()
        bp     = acct.get("buying_power", 0)
        broker_line = f"✅ {config.DATA_BROKER.capitalize()} / paper  (BP: ${bp:,.0f})"
    except Exception as e:
        broker_line = f"⚠️ {e}"

    send_message(
        f"🟢 <b>LEAP Bot — Status</b>\n"
        f"Shared DB: {'✅ Connected' if shared_ok else '❌ Not found'}\n"
        f"Positions DB: {'✅ Connected' if leap_ok else '❌ Error'}\n"
        f"Broker: {broker_line}\n"
        f"Mode: {config.BROKER_MODE}  Paper: {config.PAPER_TRADING}\n"
        f"Open trades: {open_count}\n"
        f"Last screener run: {last_run or 'never'}",
        chat_id=chat_id,
    )


def cmd_chain(args, chat_id):
    """
    /chain SYMBOL — fetch live LEAP option chain and show best candidate.
    Requires ALPACA_API_KEY/ALPACA_API_SECRET or TRADIER_API_TOKEN in .env
    """
    if not args:
        send_message("Usage: /chain SYMBOL\nExample: /chain QQQ", chat_id=chat_id)
        return

    ticker = args[0].upper()
    try:
        from broker.factory import make_broker
        from chain.leap_chain import select_leap_call, dte, breakeven
        import config
        broker = make_broker()
        price  = broker.get_latest_price(ticker)
        chain  = broker.get_option_chain(
            ticker, "call",
            min_dte=config.EXP_RANGE_MIN_DAYS,
            max_dte=config.EXP_RANGE_MAX_DAYS,
            underlying_price=price,
        )
    except Exception as e:
        send_message(f"⚠️ /chain error fetching data: {e}", chat_id=chat_id)
        return

    if not chain:
        send_message(
            f"📊 No LEAP call contracts found for {ticker} "
            f"(DTE {config.EXP_RANGE_MIN_DAYS}–{config.EXP_RANGE_MAX_DAYS})\n"
            f"Underlying: ${price:.2f}",
            chat_id=chat_id,
        )
        return

    best = select_leap_call(
        [c.to_dict() if hasattr(c, "to_dict") else c for c in chain],
        target_delta=config.LEAP_TARGET_DELTA,
        min_delta=config.LEAP_MIN_DELTA,
        max_delta=config.LEAP_MAX_DELTA,
        underlying_price=price,
        min_cost=config.LEAP_MIN_COST,
        min_open_interest=config.LEAP_MIN_OI,
        max_spread_pct=config.LEAP_MAX_SPREAD_PCT,
        max_extrinsic_pct=config.LEAP_MAX_EXTRINSIC,
    )

    total = len(chain)
    if not best:
        # Show top 5 raw contracts even if none passed filters
        lines = [f"📊 <b>{ticker}</b> — {total} LEAP contract(s) found, none passed filters\n"]
        for c in sorted(chain[:5], key=lambda x: abs((x.get("delta") or 0) - 0.80)):
            obj = c.to_dict() if hasattr(c, "to_dict") else c
            lines.append(
                f"  ${obj.get('strike',0):.0f}C  {obj.get('expiration_date')}  "
                f"δ={_fmt(obj.get('delta'))}  mid={_fmt(obj.get('mid'),'$')}"
            )
        send_message("\n".join(lines), chat_id=chat_id)
        return

    exp   = best.get("expiration_date")
    mid   = best.get("mid") or 0
    be    = breakeven(best.get("strike", 0), mid)
    days  = dte(exp) if exp else 0
    cost  = mid * 100

    send_message(
        f"📊 <b>{ticker} LEAP — Best Candidate</b>\n"
        f"Underlying: ${price:.2f}\n\n"
        f"Strike: ${best.get('strike',0):.0f}C  Exp: {exp}  ({days} DTE)\n"
        f"Delta: {_fmt(best.get('delta'))}  IV: {_fmt(best.get('implied_volatility'),suffix='%')}\n"
        f"Bid: {_fmt(best.get('bid'),'$')}  Ask: {_fmt(best.get('ask'),'$')}  Mid: {_fmt(mid,'$')}\n"
        f"Cost/contract: ${cost:.0f}  Breakeven at exp: ${be:.2f}\n"
        f"OI: {best.get('open_interest','—')}  "
        f"Symbol: <code>{best.get('symbol','—')}</code>\n\n"
        f"<i>To paper-trade this: /enter {ticker} {best.get('strike',0):.0f} {exp} {mid:.2f}</i>",
        chat_id=chat_id,
    )


# ── new broker-wired commands ─────────────────────────────────────────────────

def _fmt_preview(p, rec=None) -> str:
    """
    Render a PreviewResult into a Telegram-formatted string.
    Shared by /leap_preview and /broker_preview.
    """
    _f = lambda v, prefix="", suffix="", dec=2: (
        f"{prefix}{v:.{dec}f}{suffix}" if isinstance(v, float) else
        str(v) if v is not None else "—"
    )

    warn_block = "\n".join(f"  • {w}" for w in p.warnings) if p.warnings else "  None"

    lines = [
        f"🔍 <b>LEAP PREVIEW — {p.underlying_symbol}</b>\n",
        f"Contract: {p.underlying_symbol} {p.strike:.0f}C {p.expiration}",
        f"Mode: <b>{p.mode}</b>  Broker: {p.broker_name}\n",
        f"Bid: {_f(p.bid,'$')}  Ask: {_f(p.ask,'$')}  Mid: {_f(p.mid,'$')}",
        f"Delta: {_f(p.delta)}  IV: {_f(p.iv, suffix='%') if p.iv else '—'}",
        f"Open interest: {p.open_interest if p.open_interest else '—'}",
        f"Est. cost (1 contract): {_f(p.estimated_cost,'$')}",
        f"Breakeven: {_f(p.breakeven,'$')}\n",
    ]

    if rec:
        lines += [
            f"Screener score: {rec.get('leap_score','—')}",
            f"Trend score: {rec.get('trend_score','—')}",
            f"Risk: {rec.get('risk_rating','—')}\n",
        ]

    lines += [
        f"⚠️ <b>Warnings:</b>",
        warn_block,
    ]
    return "\n".join(lines)


def cmd_leap_preview(args, chat_id):
    """
    /leap_preview SYMBOL

    Read the most recent screener recommendation for SYMBOL, then call
    build_preview() using the screener's strike + expiration.
    Shows pricing, Greeks, warnings.  No order placed.
    """
    if not args:
        send_message("Usage: /leap_preview SYMBOL\nExample: /leap_preview QQQ", chat_id=chat_id)
        return

    ticker = args[0].upper()

    rec = sdb.get_recommendation_by_ticker(ticker)
    if not rec:
        send_message(
            f"No screener recommendation for {ticker}.\n"
            f"Run /leaps to see available candidates.",
            chat_id=chat_id,
        )
        return

    strike     = rec.get("strike") or rec.get("suggested_strike")
    expiration = rec.get("expiration") or rec.get("exp_range")

    if not strike or not expiration:
        send_message(
            f"⚠️ Screener rec for {ticker} is missing strike or expiration fields.\n"
            f"Score: {rec.get('leap_score','—')}  Run: {rec.get('run_date','—')}",
            chat_id=chat_id,
        )
        return

    # Normalise expiration — screener may store "Jan 2027" style ranges
    expiration = _normalise_expiration(expiration)

    try:
        from broker.factory import get_broker
        from broker.preview import build_preview
        broker  = get_broker()
        preview = build_preview(broker, ticker, float(strike), expiration)
    except Exception as e:
        send_message(f"⚠️ /leap_preview error fetching chain: {e}", chat_id=chat_id)
        return

    text = _fmt_preview(preview, rec)
    text += (
        f"\n\n<i>To enter paper: /enter_paper {ticker}</i>\n"
        f"<i>To enter sandbox: set BROKER_MODE=sandbox, then /broker_preview {ticker}</i>"
    )
    send_message(text, chat_id=chat_id)


def cmd_enter_paper(args, chat_id):
    """
    /enter_paper SYMBOL

    Same lookup as /leap_preview, then:
    - Calls paper_broker.place_order(preview)
    - Writes to leap_positions.db via add_paper_trade()
    - Logs to paper_orders.log
    """
    if not args:
        send_message("Usage: /enter_paper SYMBOL\nExample: /enter_paper QQQ", chat_id=chat_id)
        return

    ticker = args[0].upper()

    rec = sdb.get_recommendation_by_ticker(ticker)
    if not rec:
        send_message(
            f"No screener recommendation for {ticker}.\n"
            f"Run /leaps to see available candidates.",
            chat_id=chat_id,
        )
        return

    strike     = rec.get("strike") or rec.get("suggested_strike")
    expiration = rec.get("expiration") or rec.get("exp_range")

    if not strike or not expiration:
        send_message(
            f"⚠️ Screener rec for {ticker} is missing strike or expiration.",
            chat_id=chat_id,
        )
        return

    expiration = _normalise_expiration(expiration)

    try:
        from broker.factory import get_broker
        from broker.preview import build_preview
        from broker.order_log import log_paper_order
        broker  = get_broker()
        preview = build_preview(broker, ticker, float(strike), expiration)
    except Exception as e:
        send_message(f"⚠️ /enter_paper error building preview: {e}", chat_id=chat_id)
        return

    if not preview.mid:
        send_message(
            f"⚠️ No mid price available for {ticker} — cannot log paper trade.\n"
            + ("\n".join(f"  • {w}" for w in preview.warnings) if preview.warnings else ""),
            chat_id=chat_id,
        )
        return

    # Paper fill at mid.
    # preview.target_exit and preview.stop_loss are per-share (set in build_preview).
    # Per-contract dollar = value * 100.  DB stores per-share; display shows per-contract.
    fill_price  = preview.mid
    breakeven   = preview.breakeven or (float(strike) + fill_price)
    target_exit = preview.target_exit or round(fill_price * 2,    2)  # per-share
    stop_loss   = preview.stop_loss   or round(fill_price * 0.50, 2)  # per-share

    # Write to DB — target_exit stored per-share (consistent with entry_price column)
    try:
        trade_id = ldb.add_paper_trade(
            ticker      = ticker,
            strike      = float(strike),
            expiration  = expiration,
            entry_price = fill_price,
            breakeven   = breakeven,
            target_exit = target_exit,   # per-share, matches entry_price units
            notes       = f"paper via /enter_paper  score={rec.get('leap_score','—')}",
        )
    except Exception as e:
        send_message(f"⚠️ DB write failed: {e}", chat_id=chat_id)
        return

    # Paper broker place_order (no real API call, just mock OrderResult)
    try:
        order_result = broker.place_order(preview)
        order_id     = order_result.order_id
    except Exception:
        order_id = f"PAPER-{trade_id:05d}"

    # Log to file
    try:
        log_paper_order(
            symbol      = ticker,
            strike      = float(strike),
            expiration  = expiration,
            fill_price  = fill_price,
            order_id    = order_id,
            trade_db_id = trade_id,
        )
    except Exception as e:
        log.warning("paper_orders.log write failed: %s", e)

    _f = lambda v, prefix="": f"{prefix}{v:.2f}" if isinstance(v, float) else str(v) if v else "—"

    # Display as per-contract dollars (per-share * 100) so the user sees real cost
    send_message(
        f"📝 <b>PAPER TRADE LOGGED</b>\n\n"
        f"{ticker} {float(strike):.0f}C {expiration}\n"
        f"Fill: {_f(fill_price,'$')} (paper mid)\n"
        f"Delta: {_f(preview.delta) if preview.delta else '—'}  "
        f"IV: {f'{preview.iv:.1%}' if preview.iv else '—'}\n"
        f"Breakeven: {_f(breakeven,'$')}\n"
        f"Target: {_f(target_exit * 100,'$')}  Stop: {_f(stop_loss * 100,'$')}\n"
        f"Trade ID: {trade_id}\n"
        f"Mode: paper — no real order placed\n\n"
        f"<i>Set thesis: /thesis {trade_id} your notes</i>",
        chat_id=chat_id,
    )


def cmd_broker_preview(args, chat_id):
    """
    /broker_preview SYMBOL

    Only available when BROKER_MODE=sandbox.
    Shows a Tradier sandbox preview and prompts /confirm to submit.
    """
    import config

    mode = config.BROKER_MODE.lower()

    if mode in ("paper", "single", "alpaca"):
        send_message(
            "Set BROKER_MODE=sandbox in .env and restart to use broker preview.\n\n"
            "Current mode: paper — use /leap_preview for read-only preview.",
            chat_id=chat_id,
        )
        return

    if mode == "live":
        send_message(
            "Live mode. Use /leap_preview for read-only preview.",
            chat_id=chat_id,
        )
        return

    # mode == "sandbox" or "tradier"
    if not args:
        send_message("Usage: /broker_preview SYMBOL\nExample: /broker_preview QQQ", chat_id=chat_id)
        return

    ticker = args[0].upper()

    rec = sdb.get_recommendation_by_ticker(ticker)
    if not rec:
        send_message(
            f"No screener recommendation for {ticker}.\n"
            f"Run /leaps to see available candidates.",
            chat_id=chat_id,
        )
        return

    strike     = rec.get("strike") or rec.get("suggested_strike")
    expiration = rec.get("expiration") or rec.get("exp_range")

    if not strike or not expiration:
        send_message(
            f"⚠️ Screener rec for {ticker} is missing strike or expiration.",
            chat_id=chat_id,
        )
        return

    expiration = _normalise_expiration(expiration)

    try:
        from broker.factory import get_broker
        from broker.preview import build_preview
        broker  = get_broker()
        preview = build_preview(broker, ticker, float(strike), expiration)
    except Exception as e:
        send_message(f"⚠️ /broker_preview error: {e}", chat_id=chat_id)
        return

    text = _fmt_preview(preview, rec)
    text += (
        "\n\n⚡ <b>SANDBOX EXECUTION</b>\n"
        "This will submit a sandbox order to Tradier.\n"
        "No real money. No real routing.\n\n"
        "Reply /confirm to proceed."
    )
    send_message(text, chat_id=chat_id)

    _pending_set(chat_id, {
        "type":    "sandbox_order",
        "symbol":  ticker,
        "preview": preview,
    })


def _execute_sandbox_order(action: dict, chat_id: str) -> None:
    """Called by cmd_confirm when a sandbox_order is pending."""
    from broker.factory import get_broker
    from broker.order_log import log_sandbox_order

    preview = action["preview"]

    try:
        broker = get_broker()
        result = broker.place_order(preview)
    except Exception as e:
        send_message(f"⚠️ Sandbox order failed: {e}", chat_id=chat_id)
        return

    try:
        log_sandbox_order(
            symbol     = preview.underlying_symbol,
            strike     = preview.strike,
            expiration = preview.expiration,
            fill_price = result.filled_price,
            order_id   = result.order_id,
            status     = result.status,
        )
    except Exception as e:
        log.warning("sandbox_orders.log write failed: %s", e)

    _f = lambda v, prefix="": f"{prefix}{v:.2f}" if isinstance(v, float) else str(v)

    send_message(
        f"✅ <b>SANDBOX ORDER SUBMITTED</b>\n\n"
        f"Order ID: <code>{result.order_id}</code>\n"
        f"Status: {result.status}\n"
        f"Fill price: {_f(result.filled_price,'$')}\n"
        f"Broker: Tradier sandbox\n\n"
        f"<i>This was a sandbox order — not a real trade.</i>\n"
        f"<i>To review: lbot logs</i>",
        chat_id=chat_id,
    )


def _normalise_expiration(raw: str) -> str:
    """
    Normalise various expiration string formats to YYYY-MM-DD.

    Handles:
      "2027-01-15"        → "2027-01-15"      (already correct)
      "Jan 2027"          → "2027-01-16"      (third Friday of month)
      "2027-01"           → "2027-01-16"      (third Friday)
      "Jan-2027"          → "2027-01-16"
    """
    import re
    from datetime import date, timedelta

    raw = (raw or "").strip()

    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # YYYY-MM → find third Friday
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        return _third_friday(int(m.group(1)), int(m.group(2)))

    # "Jan 2027" or "Jan-2027"
    m = re.match(r"^([A-Za-z]{3})[\s\-](\d{4})$", raw)
    if m:
        months = {
            "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
            "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        }
        mo = months.get(m.group(1).lower())
        yr = int(m.group(2))
        if mo:
            return _third_friday(yr, mo)

    # Fallback: return as-is and let downstream handle it
    return raw


def _third_friday(year: int, month: int) -> str:
    """Return the third Friday of year/month as YYYY-MM-DD (standard options expiry)."""
    from datetime import date
    count = 0
    for day in range(1, 32):
        try:
            d = date(year, month, day)
        except ValueError:
            break
        if d.weekday() == 4:   # Friday
            count += 1
            if count == 3:
                return d.isoformat()
    return f"{year}-{month:02d}-15"  # fallback


# ── journal / scorecard ───────────────────────────────────────────────────────

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


def cmd_journal(args, chat_id):
    """/journal [N] — last N closed leap_bot trades (default 10)."""
    jdb = _load_jdb()
    if jdb is None:
        send_message("⚠️ journal_db not available.", chat_id=chat_id)
        return
    try:
        limit = int(args[0]) if args else 10
        limit = max(1, min(limit, 50))
    except (ValueError, IndexError):
        limit = 10

    trades = jdb.get_journal(limit=limit, bot_source="leaps_bot")
    open_trades = jdb.get_open_trades(bot_source="leaps_bot")

    if not trades and not open_trades:
        send_message("📒 No leap_bot trades in journal yet.", chat_id=chat_id)
        return

    lines = [f"📒 <b>LEAP JOURNAL</b> — last {limit}\n"]
    for t in trades:
        win_icon = "✅" if t.get("win") == 1 else "❌"
        pnl = t.get("pnl_dollars")
        pnl_str = f"${pnl:+.0f}" if pnl is not None else "—"
        pct = t.get("pnl_pct")
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        lines.append(
            f"#{t['id']}  {t['play_type'].replace('_', ' ').upper()}  <b>{t['symbol']}</b>\n"
            f"  Entry: {t['entry_date']}  Exit: {t.get('exit_date', '—')}\n"
            f"  P&amp;L: {pnl_str}{pct_str}  {win_icon}  [{t.get('exit_reason', '—')}]"
        )
    lines.append(f"\n<i>Open: {len(open_trades)}</i>")
    send_message("\n".join(lines), chat_id=chat_id)


def cmd_scorecard(args, chat_id):
    """/scorecard — play performance summary across all bots."""
    jdb = _load_jdb()
    if jdb is None:
        send_message("⚠️ journal_db not available.", chat_id=chat_id)
        return

    scorecard = jdb.get_scorecard()
    candidates = jdb.get_promotion_candidates()

    if not scorecard:
        send_message("📊 Scorecard empty — no closed trades logged yet.", chat_id=chat_id)
        return

    lines = ["📊 <b>LEAP SCORECARD</b>\n"]
    for row in scorecard:
        pt = row["play_type"].replace("_", " ").title()
        avg_pnl = row.get("avg_pnl_dollars") or 0
        lines.append(
            f"<b>{pt}</b>  {row['total_trades']} trades  "
            f"Win {row.get('win_rate_pct', 0):.1f}%  "
            f"Avg {'+' if avg_pnl >= 0 else ''}${avg_pnl:.0f}  "
            f"Open: {row.get('open_trades', 0)}"
        )
    if candidates:
        lines.append("\n🏆 <b>PROMOTION STATUS</b>")
        icons = {
            "READY FOR LIVE": "🟢",
            "ACCUMULATING DATA": "🟡",
            "NEED MORE TRADES": "🔴",
        }
        for c in candidates:
            icon = icons.get(c["promotion_status"], "⚪")
            pt = c["play_type"].replace("_", " ").title()
            lines.append(
                f"  {icon} {pt} ({c['total_trades']}) → {c['promotion_status']}"
            )
    send_message("\n".join(lines), chat_id=chat_id)


def cmd_leaps_status(args, chat_id):
    """/leaps_status — portfolio snapshot: open positions, capital deployed, P&L."""
    from datetime import date as _date

    open_trades = ldb.get_all_open()
    n = len(open_trades)
    capital = sum((t.get("entry_price") or 0) * 100 for t in open_trades)

    lines = [f"📊 <b>LEAPS STATUS</b>"]
    lines.append(f"Open positions: {n}")
    lines.append(f"Capital deployed: ${capital:,.0f}")

    if open_trades:
        lines.append("\n<b>Positions (up to 5):</b>")
        today = _date.today()
        for t in open_trades[:5]:
            entry = t.get("entry_price") or 0
            cur = t.get("current_price")
            if cur and entry:
                pnl_pct = ((cur - entry) / entry) * 100
                pnl_str = f"  {pnl_pct:+.1f}%"
            else:
                pnl_str = ""
            try:
                dte_rem = (_date.fromisoformat(t["expiration"]) - today).days
            except (ValueError, TypeError):
                dte_rem = None
            dte_str = f"  DTE:{dte_rem}" if dte_rem is not None else ""
            lines.append(
                f"  <b>{t['ticker']}</b> ${t['strike']:.0f} {t['expiration']}"
                f"{pnl_str}{dte_str}"
            )

    # Journal scorecard summary
    try:
        jdb = _load_jdb()
        if jdb is not None:
            scorecard = jdb.get_scorecard()
            candidates = jdb.get_promotion_candidates()
            if scorecard:
                lines.append("\n<b>Scorecard:</b>")
                for row in scorecard[:3]:
                    pt = row["play_type"].replace("_", " ").title()
                    avg = row.get("avg_pnl_dollars") or 0
                    lines.append(
                        f"  {pt}: {row['total_trades']} trades  "
                        f"Win {row.get('win_rate_pct', 0):.0f}%  "
                        f"Avg {'+' if avg >= 0 else ''}${avg:.0f}"
                    )
            if candidates:
                ready = [c for c in candidates if c.get("promotion_status") == "READY FOR LIVE"]
                if ready:
                    lines.append("\n🏆 <b>Promotion target:</b> "
                                 + ", ".join(c["play_type"].replace("_", " ").title() for c in ready))
    except Exception:
        pass

    send_message("\n".join(lines), chat_id=chat_id)


def cmd_monitor(args, chat_id):
    """/monitor — run position monitor, alert on stops/targets/DTE."""
    from monitoring.position_monitor import PositionMonitor
    from broker.factory import make_broker

    try:
        broker = make_broker()
    except Exception as e:
        send_message(f"⚠️ Broker init failed: {e}", chat_id=chat_id)
        return

    def _send(msg):
        send_message(msg, chat_id=chat_id)

    monitor = PositionMonitor(ldb, broker, _send)
    try:
        result = monitor.run()
    except Exception as e:
        send_message(f"⚠️ Monitor error: {e}", chat_id=chat_id)
        return

    alerts = result.get("alerts", 0)
    checked = result.get("checked", 0)
    if alerts == 0:
        send_message(f"✅ All {checked} position(s) healthy.", chat_id=chat_id)


# ── dispatch ──────────────────────────────────────────────────────────────────

def cmd_puts(args, chat_id):
    """List open long-put paper positions."""
    import config
    from datetime import date as _date

    rows = ldb.get_open_puts()
    if not rows:
        send_message("📭 No open put positions.", chat_id=chat_id)
        return

    today = _date.today()
    lines = [f"🐻 <b>Open Put Positions</b> ({len(rows)})\n"]
    for t in rows:
        ticker     = t["ticker"]
        strike     = t["strike"]
        entry      = t["entry_price"] or 0
        cur_price  = t.get("current_price")
        expiration = t["expiration"]

        try:
            dte_rem = (_date.fromisoformat(expiration) - today).days
        except (ValueError, TypeError):
            dte_rem = None

        # Gain estimate from current_price if available
        if cur_price and entry:
            gain_pct = (cur_price - entry) / entry
            gain_str = f"{gain_pct:+.0%}"
            cur_str  = f"  Now: ${cur_price:.2f} ({gain_str})"
        else:
            cur_str = ""

        dte_str = f"  DTE: {dte_rem}" if dte_rem is not None else ""
        lines.append(
            f"<b>{ticker}</b> ${strike:.0f}P  Entry: ${entry:.2f}"
            f"{cur_str}{dte_str}  Exp: {expiration}"
        )

    send_message("\n".join(lines), chat_id=chat_id)


def cmd_put_scan(args, chat_id):
    """
    Trigger a bearish put scan — screener candidates → best put candidate preview.
    No trade is entered; use /enter manually or extend for auto-entry if needed.
    """
    import config
    from chain.put_chain import select_put_strike, build_put_candidate

    send_message("🔍 Scanning for bearish put candidates…", chat_id=chat_id)

    # Allocation guard
    open_puts = ldb.get_open_puts()
    if len(open_puts) >= config.PUT_MAX_OPEN_POSITIONS:
        send_message(
            f"⛔ Already {len(open_puts)} open put(s) — at position limit "
            f"({config.PUT_MAX_OPEN_POSITIONS}).  Close one first.",
            chat_id=chat_id,
        )
        return

    candidates = sdb.get_put_candidates()
    if not candidates:
        send_message("📭 No bearish candidates in screener DB.", chat_id=chat_id)
        return

    try:
        from broker.factory import get_broker
        broker = get_broker()
    except Exception as e:
        send_message(f"⚠️ Broker init failed: {e}", chat_id=chat_id)
        return

    selected = None
    for cand in candidates[:10]:
        symbol = cand.get("symbol", "")
        if not symbol:
            continue
        try:
            price = broker.get_latest_price(symbol)
            if not price:
                continue
            chain = broker.get_option_chain(
                symbol, "put",
                min_dte=config.PUT_EXP_MIN_DAYS,
                max_dte=config.PUT_EXP_MAX_DAYS,
                underlying_price=price,
            )
            if not chain:
                continue
            chain_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chain]
            best = select_put_strike(
                chain_dicts,
                target_delta=config.PUT_TARGET_DELTA,
                min_delta=config.PUT_MIN_DELTA,
                max_delta=config.PUT_MAX_DELTA,
                underlying_price=price,
                min_cost=config.PUT_MIN_COST,
                min_open_interest=config.PUT_MIN_OI,
                max_spread_pct=config.PUT_MAX_SPREAD_PCT,
                max_extrinsic_pct=config.PUT_MAX_EXTRINSIC,
            )
            if best:
                selected = (symbol, price, build_put_candidate(best, symbol, price))
                break
        except Exception:
            continue

    if not selected:
        send_message("📭 No candidates passed put filters.", chat_id=chat_id)
        return

    symbol, price, put = selected
    mid = put.mid_price or 0
    cost = round(mid * 100, 2)

    iv_str = f"{put.iv*100:.0f}%" if put.iv else "—"
    msg = (
        f"🐻 <b>PUT CANDIDATE — {symbol}</b>\n"
        f"Underlying: ${price:.2f}\n"
        f"Strike: ${put.strike:.0f}P  Exp: {put.expiration}  DTE: {put.dte}\n"
        f"Delta: {_fmt(put.delta)}  IV: {iv_str}\n"
        f"Bid/Ask: ${_fmt(put.bid)} / ${_fmt(put.ask)}\n"
        f"Mid: ${mid:.2f} (${cost:.0f}/contract)\n"
        f"Intrinsic: ${put.intrinsic:.2f}  Extrinsic: ${put.extrinsic:.2f}\n"
        f"Breakeven: ${put.breakeven:.2f}\n"
        f"Target: ${put.target_exit:.2f}/share (${put.target_exit*100:.0f}/contract)\n"
        f"Stop: ${put.stop_loss:.2f}/share (${put.stop_loss*100:.0f}/contract)\n"
        f"OI: {put.open_interest}\n\n"
        f"To enter: /enter {symbol} {put.strike:.0f} {put.expiration} {mid:.2f}"
    )
    send_message(msg, chat_id=chat_id)


def cmd_risk(args, chat_id):
    """Show portfolio risk snapshot from the risk engine."""
    try:
        import config
        from risk_engine import RiskEngine
        re = RiskEngine(
            account_size   = 100_000,
            shared_db_path = config.SHARED_DB_PATH,
            leaps_db_path  = config.LEAP_DB_PATH,
        )
        snap = re.get_portfolio_snapshot()
        cb = snap.get("circuit_breaker")
        cb_line = f"\n⛔ Circuit breaker: {cb}" if cb else ""
        msg = (
            f"📊 *Risk Snapshot*\n\n"
            f"Heat: ${snap['heat_dollars']:,.0f}  ({snap['heat_pct']*100:.1f}%)  {snap['heat_status']}\n"
            f"Delta: {snap['portfolio_delta']:+.0f}  {snap['delta_status']}\n"
            f"MTD P&L: ${snap['mtd_pnl']:+,.0f}  ({snap['mtd_pct']*100:.1f}%)  {snap['drawdown_status']}\n"
            f"Consecutive losses: {snap['consecutive_losses']}{cb_line}"
        )
        send_message(msg, chat_id=chat_id)
    except Exception as e:
        send_message(f"⚠️ Risk engine error: {e}", chat_id=chat_id)


def cmd_pause(args, chat_id):
    """Manually trigger circuit breaker (48-hour pause)."""
    try:
        import config
        from risk_engine import RiskEngine
        from pathlib import Path
        from datetime import datetime, timedelta
        cb_file = Path.home() / ".zulucare_circuit_breaker"
        pause_until = datetime.now() + timedelta(hours=48)
        cb_file.write_text(pause_until.isoformat())
        send_message(f"⛔ Manual pause activated — trading paused until {pause_until.strftime('%Y-%m-%d %H:%M')}.\nUse /resume to clear.", chat_id=chat_id)
    except Exception as e:
        send_message(f"⚠️ Pause error: {e}", chat_id=chat_id)


def cmd_resume(args, chat_id):
    """Clear circuit breaker and resume trading."""
    try:
        import config
        from risk_engine import RiskEngine
        re = RiskEngine(
            account_size   = 100_000,
            shared_db_path = config.SHARED_DB_PATH,
            leaps_db_path  = config.LEAP_DB_PATH,
        )
        result = re.resume_circuit_breaker()
        send_message(f"✅ {result}", chat_id=chat_id)
    except Exception as e:
        send_message(f"⚠️ Resume error: {e}", chat_id=chat_id)


COMMANDS = {
    "/help":            cmd_help,
    "/leaps":           cmd_leaps,
    "/watchlist":       cmd_watchlist,
    "/confirm":         cmd_confirm,
    "/enter":           cmd_enter,
    "/close":           cmd_close,
    "/update":          cmd_update,
    "/trades":          cmd_trades,
    "/pnl":             cmd_pnl,
    "/status":          cmd_status,
    "/chain":           cmd_chain,
    "/leap_preview":    cmd_leap_preview,
    "/enter_paper":     cmd_enter_paper,
    "/broker_preview":  cmd_broker_preview,
    "/journal":         cmd_journal,
    "/scorecard":       cmd_scorecard,
    "/puts":            cmd_puts,
    "/put_scan":        cmd_put_scan,
    "/leaps_status":    cmd_leaps_status,
    "/monitor":         cmd_monitor,
    "/risk":            cmd_risk,
    "/pause":           cmd_pause,
    "/resume":          cmd_resume,
}


def _handle_message(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()

    if not text or not chat_id:
        return
    if chat_id != str(TELEGRAM_CHAT_ID):
        return  # silently ignore unauthorized chats

    parts = text.split()
    raw_cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    handler = COMMANDS.get(raw_cmd)
    if not handler:
        return  # unknown command — ignore

    try:
        handler(args, chat_id)
    except Exception as e:
        log.exception("Error in %s", raw_cmd)
        send_message(f"⚠️ {raw_cmd} error: {e}", chat_id=chat_id)


# ── polling loop ──────────────────────────────────────────────────────────────

def run():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("LEAP_BOT_TOKEN not set")

    log.info("LEAP Bot listening...")
    print("LEAP Bot listening...")

    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=40)
            resp.raise_for_status()
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    _handle_message(msg)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error("Polling error: %s", e)
            time.sleep(5)
