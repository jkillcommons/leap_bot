import argparse
import sys
import os

# ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import db.shared_db as sdb
import db.leap_db as ldb


def _fmt(val, prefix="", suffix="", decimals=2):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{prefix}{val:.{decimals}f}{suffix}"
    return f"{prefix}{val}{suffix}"


def print_table(rows, columns):
    if not rows:
        return False
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(row.get(c) or "—")))
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    sep    = "  ".join("-" * widths[c] for c in columns)
    print(header)
    print(sep)
    for row in rows:
        print("  ".join(str(row.get(c) or "—").ljust(widths[c]) for c in columns))
    return True


def mode_leaps():
    recs = sdb.get_leap_recommendations(20)
    if not recs:
        print("No LEAP recommendations in screener DB yet.")
        return
    cols = ["ticker", "leap_score", "suggested_delta", "strike", "exp_range",
            "ask_price", "mid_price", "breakeven", "iv_level",
            "trend_score", "risk_rating", "run_date"]
    print(f"\n{'='*60}")
    print("  LEAP Recommendations")
    print(f"{'='*60}")
    print_table(recs, cols)
    print()


def mode_watchlist():
    rows = sdb.get_watchlist()
    if not rows:
        print("LEAP watchlist is empty.")
        return
    tier1 = [r for r in rows if r.get("tier") == 1]
    tier2 = [r for r in rows if r.get("tier") != 1]
    cols = ["symbol", "strategy", "tier", "date_added", "added_by"]

    print(f"\n{'='*60}")
    print("  LEAP Watchlist — Tier 1 Active")
    print(f"{'='*60}")
    if tier1:
        print_table(tier1, cols)
    else:
        print("  (none)")

    print(f"\n{'='*60}")
    print("  LEAP Watchlist — Tier 2 Monitor")
    print(f"{'='*60}")
    if tier2:
        print_table(tier2, cols)
    else:
        print("  (none)")
    print()


def mode_trades():
    rows = ldb.get_open_trades()
    if not rows:
        print("No open paper trades.")
        return
    cols = ["ticker", "strike", "expiration", "contracts",
            "entry_price", "current_price", "breakeven", "entered_date", "notes"]
    print(f"\n{'='*60}")
    print("  Open LEAP Paper Positions")
    print(f"{'='*60}")
    print_table(rows, cols)
    print()


def mode_summary():
    s = ldb.get_trade_summary()
    if s["total_closed"] == 0:
        print("No closed trades yet.")
        return
    print(f"\n{'='*40}")
    print("  LEAP P&L Summary")
    print(f"{'='*40}")
    print(f"  Open trades  : {s['total_open']}")
    print(f"  Closed trades: {s['total_closed']}")
    print(f"  Total P&L    : ${s['total_pnl']:.2f}")
    print(f"  Win rate     : {s['win_rate']}%")
    print(f"  Avg per trade: ${s['avg_pnl_per_trade']:.2f}")
    print(f"  Best trade   : ${s['best_trade']:.2f}")
    print(f"  Worst trade  : ${s['worst_trade']:.2f}")
    print()


def mode_listen():
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(
                os.path.join(os.path.dirname(__file__), "logs", "listener.log")
            ),
            logging.StreamHandler(),
        ],
    )
    from reporting.telegram_listener import run
    run()


def mode_chain(symbol: str):
    """Fetch live option chain and print best LEAP candidate."""
    import config
    from broker.factory import make_broker
    from chain.leap_chain import select_leap_call, dte, breakeven, extrinsic_value

    print(f"\n{'='*64}")
    print(f"  LEAP Chain — {symbol}")
    print(f"{'='*64}")

    try:
        broker = make_broker()
    except Exception as e:
        print(f"  Broker init failed: {e}")
        return

    price = broker.get_latest_price(symbol)
    print(f"  Underlying: ${price:.2f}")
    print(f"  DTE window: {config.EXP_RANGE_MIN_DAYS}–{config.EXP_RANGE_MAX_DAYS} days")

    chain = broker.get_option_chain(
        symbol, "call",
        min_dte=config.EXP_RANGE_MIN_DAYS,
        max_dte=config.EXP_RANGE_MAX_DAYS,
        underlying_price=price,
    )

    if not chain:
        print(f"  No LEAP contracts found.")
        print()
        return

    print(f"  {len(chain)} contract(s) in window\n")

    chain_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chain]
    best = select_leap_call(
        chain_dicts,
        target_delta=config.LEAP_TARGET_DELTA,
        min_delta=config.LEAP_MIN_DELTA,
        max_delta=config.LEAP_MAX_DELTA,
        underlying_price=price,
        min_cost=config.LEAP_MIN_COST,
        min_open_interest=config.LEAP_MIN_OI,
        max_spread_pct=config.LEAP_MAX_SPREAD_PCT,
        max_extrinsic_pct=config.LEAP_MAX_EXTRINSIC,
    )

    if not best:
        print("  No candidate passed all filters (see diagnostics above).")
        print()
        return

    mid  = best.get("mid") or 0
    be   = breakeven(best.get("strike", 0), mid)
    ext  = extrinsic_value(mid, best.get("strike", 0), price) if price else 0
    days = dte(best["expiration_date"]) if best.get("expiration_date") else 0

    print("  ✅ BEST CANDIDATE")
    print(f"  Symbol    : {best.get('symbol','—')}")
    print(f"  Strike    : ${best.get('strike',0):.0f}C")
    print(f"  Expiration: {best.get('expiration_date')}  ({days} DTE)")
    print(f"  Delta     : {best.get('delta','—')}")
    print(f"  IV        : {best.get('implied_volatility','—')}")
    print(f"  Bid/Ask   : ${best.get('bid','—')} / ${best.get('ask','—')}")
    print(f"  Mid       : ${mid:.2f}  (${mid*100:.0f}/contract)")
    print(f"  Intrinsic : ${max(price - best.get('strike',0), 0):.2f}")
    print(f"  Extrinsic : ${ext:.2f}  ({ext/mid*100:.0f}% of mid)" if mid else "")
    print(f"  Breakeven : ${be:.2f}  at expiration")
    print(f"  OI        : {best.get('open_interest','—')}")
    print()
    print(f"  To paper-trade: python3 main.py (via /enter in Telegram)")
    print()


def mode_puts(dry_run=False):
    """Scan for bearish put candidates and paper-trade the best one."""
    import config
    from chain.put_chain import select_put_strike, build_put_candidate
    from broker.factory import make_broker

    print(f"\n{'='*64}")
    print("  Long Put Scan" + (" (DRY RUN)" if dry_run else ""))
    print(f"{'='*64}")

    # ── Allocation guard ──────────────────────────────────────────────────────
    open_puts = ldb.get_open_puts()
    if len(open_puts) >= config.PUT_MAX_OPEN_POSITIONS:
        print(f"\n  ⛔  Already {len(open_puts)} open put(s) — at position limit "
              f"({config.PUT_MAX_OPEN_POSITIONS}).  Close a position first.\n")
        return

    # ── Candidates ────────────────────────────────────────────────────────────
    candidates = sdb.get_put_candidates()
    if not candidates:
        print("  No bearish candidates found in screener DB.\n")
        return

    print(f"  {len(candidates)} candidate(s) with wheel_score < 55\n")

    try:
        broker = make_broker()
    except Exception as e:
        print(f"  Broker init failed: {e}\n")
        return

    selected = None
    for cand in candidates[:10]:          # cap at first 10 to avoid rate-limit spam
        symbol = cand.get("symbol", "")
        if not symbol:
            continue
        print(f"  Checking {symbol}…")
        try:
            price = broker.get_latest_price(symbol)
            if not price:
                print(f"    skip — no price")
                continue
            chain = broker.get_option_chain(
                symbol, "put",
                min_dte=config.PUT_EXP_MIN_DAYS,
                max_dte=config.PUT_EXP_MAX_DAYS,
                underlying_price=price,
            )
            if not chain:
                print(f"    skip — no chain data")
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
                selected = (symbol, price, build_put_candidate(best, price))
                break
        except Exception as e:
            print(f"    error: {e}")
            continue

    if not selected:
        print("\n  No candidate passed all put filters.\n")
        return

    symbol, price, put = selected
    mid = put.mid_price or 0

    print(f"\n  ✅ BEST PUT CANDIDATE")
    print(f"  Symbol    : {symbol}")
    print(f"  Underlying: ${price:.2f}")
    print(f"  Strike    : ${put.strike:.0f}P")
    print(f"  Expiration: {put.expiration_date}  ({put.dte} DTE)")
    print(f"  Delta     : {_fmt(put.delta)}")
    print(f"  IV        : {_fmt(put.implied_volatility, suffix='%') if put.implied_volatility else '—'}")
    print(f"  Bid/Ask   : ${_fmt(put.bid)} / ${_fmt(put.ask)}")
    print(f"  Mid       : ${mid:.2f}  (${mid*100:.0f}/contract)")
    print(f"  Intrinsic : ${put.intrinsic:.2f}")
    print(f"  Extrinsic : ${put.extrinsic:.2f}  ({put.extrinsic/mid*100:.0f}% of mid)" if mid else "")
    print(f"  Breakeven : ${put.breakeven:.2f}  at expiration")
    print(f"  Target    : ${put.target_exit:.2f}/share  (${put.target_exit*100:.0f}/contract)")
    print(f"  Stop      : ${put.stop_loss:.2f}/share  (${put.stop_loss*100:.0f}/contract)")
    print(f"  OI        : {put.open_interest}")

    if dry_run:
        print("\n  DRY RUN — no trade recorded.\n")
        return

    # ── Interactive confirm ───────────────────────────────────────────────────
    try:
        answer = input(f"\n  Paper-trade {symbol} ${put.strike:.0f}P @ ${mid:.2f}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer != "y":
        print("  Aborted.\n")
        return

    breakeven_val = round(put.strike - mid, 2)
    trade_id = ldb.add_paper_trade(
        ticker      = symbol,
        strike      = put.strike,
        expiration  = str(put.expiration_date)[:10],
        entry_price = mid,
        breakeven   = breakeven_val,
        target_exit = put.target_exit,
        notes       = f"put scan; delta={put.delta}; iv={put.implied_volatility}",
        contracts   = 1,
        play_type   = "long_put",
    )
    print(f"\n  ✅ Paper trade logged (id={trade_id}).")
    print(f"  Breakeven: ${breakeven_val:.2f} | Target: ${put.target_exit:.2f}/share\n")


def mode_check_puts():
    """Check open put positions for profit/loss/time exits and close as needed."""
    import config
    from broker.factory import make_broker
    from chain.put_chain import put_breakeven

    open_puts = ldb.get_open_puts()
    if not open_puts:
        print("No open put positions.")
        return

    print(f"\n{'='*64}")
    print("  Put Exit Monitor")
    print(f"{'='*64}")

    try:
        broker = make_broker()
    except Exception as e:
        print(f"  Broker init failed: {e}\n")
        return

    from datetime import date as _date
    today = _date.today()
    closed = 0

    for trade in open_puts:
        ticker     = trade["ticker"]
        entry      = trade["entry_price"]
        target     = trade["target_exit"]     # per-share
        contracts  = trade["contracts"] or 1
        expiration = trade["expiration"]

        try:
            dte_remaining = (_date.fromisoformat(expiration) - today).days
        except (ValueError, TypeError):
            dte_remaining = 999

        # Fetch current option mid from broker
        try:
            chain = broker.get_option_chain(
                ticker, "put",
                min_dte=0,
                max_dte=config.PUT_EXP_MAX_DAYS + 30,
            )
            chain_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in chain]
            match = next(
                (c for c in chain_dicts
                 if abs(c.get("strike", 0) - trade["strike"]) < 0.01
                 and str(c.get("expiration_date", "")).startswith(expiration[:7])),
                None,
            )
            current_mid = match.get("mid") if match else None
        except Exception:
            current_mid = None

        if current_mid is None:
            print(f"  {ticker}: no current price — skipping")
            continue

        gain_pct = (current_mid - entry) / entry if entry else 0
        pnl_est  = (current_mid - entry) * 100 * contracts

        exit_reason = None
        if gain_pct >= config.PUT_TARGET_GAIN:
            exit_reason = f"profit target hit ({gain_pct:.0%})"
        elif gain_pct <= -config.PUT_MAX_LOSS:
            exit_reason = f"stop loss hit ({gain_pct:.0%})"
        elif dte_remaining <= config.PUT_MAX_DTE_EXIT:
            exit_reason = f"time exit ({dte_remaining} DTE remaining)"

        status_line = (f"  {ticker} ${trade['strike']:.0f}P  "
                       f"entry=${entry:.2f}  now=${current_mid:.2f}  "
                       f"gain={gain_pct:+.0%}  DTE={dte_remaining}")

        if exit_reason:
            result = ldb.close_trade(ticker, current_mid, notes=exit_reason)
            print(f"{status_line}")
            print(f"    ⚠️  CLOSED — {exit_reason}  P&L=${pnl_est:+.2f}")
            closed += 1

            # Telegram notification (non-fatal)
            try:
                import config as _cfg
                if _cfg.TELEGRAM_BOT_TOKEN and _cfg.TELEGRAM_CHAT_ID:
                    import urllib.request, urllib.parse
                    msg = (f"🔔 Put Exit\n{ticker} ${trade['strike']:.0f}P\n"
                           f"{exit_reason}\nEntry: ${entry:.2f}  Exit: ${current_mid:.2f}\n"
                           f"P&L: ${pnl_est:+.2f}")
                    urllib.request.urlopen(
                        f"https://api.telegram.org/bot{_cfg.TELEGRAM_BOT_TOKEN}/sendMessage?"
                        + urllib.parse.urlencode({"chat_id": _cfg.TELEGRAM_CHAT_ID, "text": msg}),
                        timeout=10,
                    )
            except Exception:
                pass
        else:
            print(f"{status_line}  — holding")

    print(f"\n  {closed} position(s) closed.\n")


def mode_broker():
    """Show broker connection status."""
    import config
    from broker.factory import make_broker

    print(f"\n{'='*50}")
    print("  Broker Status")
    print(f"{'='*50}")
    print(f"  Mode       : {config.BROKER_MODE}")
    print(f"  Data broker: {config.DATA_BROKER}")
    print(f"  Exec broker: {config.EXEC_BROKER}")
    print(f"  Paper      : {config.PAPER_TRADING}")
    print()

    try:
        broker = make_broker()
        acct   = broker.get_account()
        print(f"  ✅ Connected")
        print(f"  Buying power   : ${acct.get('buying_power', 0):,.2f}")
        print(f"  Cash           : ${acct.get('cash', 0):,.2f}")
        print(f"  Portfolio value: ${acct.get('portfolio_value', 0):,.2f}")
    except Exception as e:
        print(f"  ❌ Connection failed: {e}")
    print()


MODES = {
    "leaps":      lambda: mode_leaps(),
    "watchlist":  lambda: mode_watchlist(),
    "trades":     lambda: mode_trades(),
    "summary":    lambda: mode_summary(),
    "listen":     lambda: mode_listen(),
    "chain":      None,          # handled specially — needs --symbol
    "broker":     lambda: mode_broker(),
    "puts":       None,          # handled specially — supports --dry-run
    "check_puts": lambda: mode_check_puts(),
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LEAP Bot")
    parser.add_argument(
        "--mode",
        choices=list(MODES.keys()),
        required=True,
        help="Run mode",
    )
    parser.add_argument(
        "--symbol", "-s",
        default=None,
        help="Symbol for --mode chain",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan without writing any trades (puts mode)",
    )
    args = parser.parse_args()

    if args.mode == "chain":
        if not args.symbol:
            parser.error("--symbol is required for --mode chain")
        mode_chain(args.symbol.upper())
    elif args.mode == "puts":
        mode_puts(dry_run=args.dry_run)
    else:
        MODES[args.mode]()
