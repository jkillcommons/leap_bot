import os

# ── Paths ─────────────────────────────────────────────────────────────────────
SHARED_DB_PATH = os.path.expanduser("~/shared_data/wheel_research.db")
LEAP_DB_PATH   = os.path.expanduser("~/leap_bot/data/leap_positions.db")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("LEAP_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ── Screener thresholds ───────────────────────────────────────────────────────
LEAP_SCORE_MIN       = 75
TREND_SCORE_MIN      = 70
SUGGESTED_DELTA_LOW  = 0.70
SUGGESTED_DELTA_HIGH = 0.85
EXP_RANGE_MIN_MONTHS = 12
EXP_RANGE_MAX_MONTHS = 24

# Derived DTE constants (used by broker calls)
EXP_RANGE_MIN_DAYS = EXP_RANGE_MIN_MONTHS * 30   # ~365
EXP_RANGE_MAX_DAYS = EXP_RANGE_MAX_MONTHS * 30   # ~720

# ── Broker selection ──────────────────────────────────────────────────────────
# DATA_BROKER    : "alpaca" | "tradier"
#   Source for prices, historical closes, option chains (with Greeks)
#   Tradier sandbox has better LEAP chain coverage; Alpaca works for paper testing
#
# EXEC_BROKER    : "alpaca" (only supported execution venue)
#
# BROKER_MODE    : "single" | "dual"
#   "single" — use DATA_BROKER for everything (no execution in read-only phase)
#   "dual"   — Tradier data + Alpaca execution (for live/paper order placement)
#
# PAPER_TRADING  : must stay True until you explicitly choose to go live
#
# Override via env vars DATA_BROKER, EXEC_BROKER, BROKER_MODE, PAPER_TRADING

DATA_BROKER   = os.getenv("DATA_BROKER",  "alpaca")
EXEC_BROKER   = os.getenv("EXEC_BROKER",  "alpaca")
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# BROKER_MODE valid values:
#   paper    — AlpacaBroker paper=True (mock if no keys set)  ← default
#   sandbox  — TradierClient → sandbox.tradier.com (real sandbox orders)
#   dual     — Tradier data + Alpaca paper execution
#   single   — alias for paper (backwards-compat)
BROKER_MODE   = os.getenv("BROKER_MODE",  "paper")

# Tradier account ID — required for sandbox order placement
# Find at https://developer.tradier.com/user/profile
TRADIER_ACCOUNT_ID = os.getenv("TRADIER_ACCOUNT_ID", "")

# ── LEAP strategy parameters ──────────────────────────────────────────────────
LEAP_TARGET_DELTA   = 0.80   # ideal delta for strike selection
LEAP_MIN_DELTA      = 0.70   # hard floor
LEAP_MAX_DELTA      = 0.90   # hard ceiling
LEAP_MIN_COST       = 5.00   # minimum mid price per share ($500/contract)
LEAP_MIN_OI         = 50     # minimum open interest
LEAP_MAX_SPREAD_PCT = 0.10   # max bid/ask spread as fraction of mid
LEAP_MAX_EXTRINSIC  = 0.30   # max extrinsic value as fraction of mid
LEAP_TARGET_GAIN    = 1.00   # exit target: 100% gain (2x entry)
LEAP_MAX_LOSS       = 0.50   # stop loss: cut at 50% loss

# ── Long put strategy parameters ─────────────────────────────────────────────
PUT_MAX_OPEN_POSITIONS = 2        # never hold more than 2 open puts at once
PUT_MAX_CAPITAL_PCT    = 0.20     # max 20% of paper account in puts

PUT_TARGET_DELTA = -0.70          # ideal put delta (negative)
PUT_MIN_DELTA    = -0.80          # most negative allowed
PUT_MAX_DELTA    = -0.60          # least negative allowed

PUT_EXP_MIN_DAYS = 45             # minimum DTE for put selection
PUT_EXP_MAX_DAYS = 180            # maximum DTE for put selection

PUT_MIN_COST       = 2.00         # minimum mid price per share ($200/contract)
PUT_MIN_OI         = 50           # minimum open interest
PUT_MAX_SPREAD_PCT = 0.10         # max bid/ask spread as fraction of mid
PUT_MAX_EXTRINSIC  = 0.40         # max extrinsic as fraction of mid (puts have more)

PUT_TARGET_GAIN = 0.75            # exit target: 75% gain
PUT_MAX_LOSS    = 0.40            # stop loss: cut at 40% loss
PUT_MAX_DTE_EXIT = 7              # time exit: close when ≤ 7 DTE remains
