# LEAP Bot — Changelog

## 2026-06-04 — Alpaca Removal / Tradier Migration

### Summary
Removed Alpaca as broker. TradierClient is now the sole broker for all
market data and paper execution. No functional change to trading logic.

### Files deleted
- `broker/alpaca_client.py` — AlpacaBroker class, Alpaca SDK imports, BS math helpers

### Files added
- `broker/math_helpers.py` — Black-Scholes helpers extracted from alpaca_client.py
  (`_compute_hist_vol`, `_bs_call_delta`, `_bs_call_price`)
- `monitoring/position_monitor.py` — Daily position health check (stop/target/DTE alerts)
- `monitoring/__init__.py`
- `launch_listener.sh` — launchd wrapper that sources .env before starting listener
- `launch_monitor.sh` — launchd wrapper for daily monitor job

### Files modified
- `broker/tradier_client.py`
  - Implemented `get_account()` via `GET /accounts/{id}/balances`
  - Implemented `get_positions()` via `GET /accounts/{id}/positions`
  - Implemented `get_open_orders()` via `GET /accounts/{id}/orders`
  - Implemented `buy_to_open_call()` and `sell_to_close_call()`
  - Removed all `raise NotImplementedError("Use AlpacaBroker...")` stubs
- `broker/factory.py`
  - All BROKER_MODE values now return `TradierClient()`
  - Removed `AlpacaBroker` import and `DualBroker` usage
  - `PAPER_TRADING=True` guard retained
- `broker/dual_broker.py` — Docstring updated, Alpaca references removed
- `broker/preview.py` — `broker_name` default changed from `"alpaca"` to `"tradier"`
- `config.py`
  - `DATA_BROKER` default: `"alpaca"` → `"tradier"`
  - `EXEC_BROKER` default: `"alpaca"` → `"tradier"`
  - `BROKER_MODE` default: `"paper"` → `"sandbox"`
  - Added `PAPER_BUYING_POWER = 1_000_000` override
- `reporting/telegram_listener.py`
  - Removed `"alpaca"` from broker mode string checks
  - Updated docstrings removing ALPACA_API_KEY references
  - Added `/leaps_status`, `/monitor` commands
  - Upgraded `/enter` with stop-loss and target display
  - Upgraded `/close` with exit_reason derivation
- `db/leap_db.py`
  - Added `delta_at_entry`, `iv_rank_at_entry`, `stop_loss_price` columns
  - Added `set_journal_id()` and `get_journal_id()` helpers
  - `close_trade()` accepts `exit_reason` param, passes to journal
- `requirements.txt` — Removed `alpaca-py`
- `.env.example` — Removed Alpaca section, updated defaults to tradier/sandbox
- `lbot` — Rewired `start/stop/status` to use launchd instead of PID file

### Known gaps (not replaced)
| Alpaca capability | Status |
|---|---|
| `get_option_mid()` — single contract mid-price lookup | Dropped (dead code, zero call sites) |
| MOCK mode (synthetic chain when no keys set) | Dropped — Tradier requires a real sandbox token |
| `_bs_fallback_symbols` tracking | Dropped — Tradier provides live Greeks, no BS fallback needed |
| Real-time brokerage position reconciliation | Never used — positions tracked in leap_positions.db only |

### Environment changes (Mac mini .env)
```
# Removed
ALPACA_API_KEY
ALPACA_API_SECRET
ALPACA_BASE_URL

# Changed
EXEC_BROKER=alpaca  →  EXEC_BROKER=tradier
DATA_BROKER=alpaca  →  DATA_BROKER=tradier
BROKER_MODE=dual    →  BROKER_MODE=sandbox
```

### TradierClient methods now in use
| Method | Called from | Purpose |
|---|---|---|
| `get_latest_price(symbol)` | main.py, telegram_listener, preview, monitor | Live stock quote |
| `get_historical_closes(symbol, days)` | chain/leap_chain.py | Historical vol calc |
| `get_option_chain(symbol, type, min_dte, max_dte)` | main.py, telegram_listener, preview, monitor | LEAP/put chain fetch with live Greeks |
| `get_account()` | main.py, telegram_listener | Buying power display |
| `get_positions()` | Available, not yet called | Brokerage position reconciliation |
| `get_open_orders()` | Available, not yet called | Order status |
| `place_order(preview)` | telegram_listener `/broker_preview` + `/confirm` | Sandbox order submission |
| `buy_to_open_call(symbol, contracts, price)` | Available via place_order | BTO wrapper |
| `sell_to_close_call(symbol, contracts, price)` | Available | STC wrapper |

### LaunchAgents (Mac mini)
| Plist | Label | Fires | Entry point |
|---|---|---|---|
| `com.leapbot.plist` | com.leapbot | On boot + keep-alive | `launch_listener.sh` → `main.py --mode listen` |
| `com.zulucare.leaps_monitor.plist` | com.zulucare.leaps_monitor | Daily 4:15pm ET | `launch_monitor.sh` → `main.py --mode monitor` |

### Isolated — not touched
- `wheel_bot/` — still uses AlpacaBroker for paper execution (separate migration)
- `research_bot/report_server.py` — pulls LEAP account data via `LEAP_ALPACA_KEY`
  (will show $0 for LEAP account until report_server is migrated to Tradier)

---

## Prior history — see git log
