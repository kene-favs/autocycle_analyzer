"""
autocycle_broker/config.py
──────────────────────────
Settings for the Autocycle AI Broker service.
All credentials and schedules come from .env — zero code changes
needed to switch demo→live or to enable/disable the dual schedule.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ────────────────────────────────────────────────────────────────────────────
# SINGLE-SYMBOL mode (default)
# Set MT5_SYMBOL and CONTRACT_SIZE in .env.
# ────────────────────────────────────────────────────────────────────────────

SYMBOL        = os.getenv('MT5_SYMBOL',    'XAUUSD+')
CONTRACT_SIZE = float(os.getenv('CONTRACT_SIZE', '100'))   # 100 for gold, 1 for BTC

# ────────────────────────────────────────────────────────────────────────────
# DUAL-SYMBOL SCHEDULE  (set ENABLE_SCHEDULE=true in .env to activate)
#
# Gold  : Mon–Fri  05:00–21:00 London  → XAUUSD+ (London + NY session)
# BTC   : Mon–Fri  21:00–05:00 London  → BTCUSD  (Asian session night)
#          + all weekend (Sat 00:00 → Mon 05:00)
#
# When enabled, the bot never sits idle.  Between cycles, the broker
# checks the clock and picks the right instrument automatically.
# ────────────────────────────────────────────────────────────────────────────

ENABLE_SCHEDULE  = os.getenv('ENABLE_SCHEDULE',  'false').lower() == 'true'
CYCLES_ENABLED   = os.getenv('CYCLES_ENABLED',  'true').lower()  == 'true'  # set false to pause all new cycle opens

# ── Gold profile ─────────────────────────────────────────────────────────────
GOLD_SYMBOL      = os.getenv('GOLD_SYMBOL',    'XAUUSD+')
GOLD_CONTRACT    = float(os.getenv('GOLD_CONTRACT', '100'))   # 100 oz/lot
GOLD_START_HOUR  = int(os.getenv('GOLD_START',  '5'))         # 05:00 London
GOLD_END_HOUR    = int(os.getenv('GOLD_END',   '21'))         # 21:00 London
GOLD_ATR_MIN     = float(os.getenv('GOLD_ATR_MIN',  '1.00'))
GOLD_SL_MIN      = float(os.getenv('GOLD_SL_MIN',   '1.50'))
GOLD_SL_MAX      = float(os.getenv('GOLD_SL_MAX',   '1.50'))  # SL fixed at $1.50 regardless of ATR
GOLD_TP_MIN      = float(os.getenv('GOLD_TP_MIN',   '0.30'))
GOLD_TP_MAX      = float(os.getenv('GOLD_TP_MAX',   '0.40'))

# ── BTC profile ──────────────────────────────────────────────────────────────
# P&L math: SL=200 price dist × 0.01 lot × CS=1 = $2.00 — same as gold $2 SL
# ATR for BTC M1 candles is in raw $ (e.g. $50–$300 per minute bar)
BTC_SYMBOL       = os.getenv('BTC_SYMBOL',     'BTCUSD')
BTC_CONTRACT     = float(os.getenv('BTC_CONTRACT',  '1'))     # 1 BTC/lot
BTC_ATR_MIN      = float(os.getenv('BTC_ATR_MIN',  '30'))     # skip if M1 ATR < $30
BTC_SL_MIN       = float(os.getenv('BTC_SL_MIN',  '200'))     # $200 dist → $2.00 @ 0.01 lot
BTC_SL_MAX       = float(os.getenv('BTC_SL_MAX',  '500'))     # $500 dist → $5.00 @ 0.01 lot
BTC_TP_MIN       = float(os.getenv('BTC_TP_MIN',   '30'))     # $30  dist → $0.30 @ 0.01 lot
BTC_TP_MAX       = float(os.getenv('BTC_TP_MAX',  '100'))     # $100 dist → $1.00 @ 0.01 lot

# ────────────────────────────────────────────────────────────────────────────
# ATR and SL/TP (used in single-symbol mode; overridden by profiles above)
# ────────────────────────────────────────────────────────────────────────────

ATR_PERIOD    = 14
ATR_MIN       = float(os.getenv('ATR_MIN',      '1.00'))
SL_ATR_MULT   = 1.50    # SL distance = ATR × 1.50
TP_EXTRA_MULT = 0.25    # TP extension = ATR × 0.25
SL_MIN        = float(os.getenv('SL_MIN',      '1.50'))
SL_MAX        = float(os.getenv('SL_MAX',      '1.50'))  # SL fixed at $1.50 regardless of ATR
TP_EXTRA_MIN  = float(os.getenv('TP_EXTRA_MIN', '0.30'))
TP_EXTRA_MAX  = float(os.getenv('TP_EXTRA_MAX', '0.40'))

# ────────────────────────────────────────────────────────────────────────────
# Internal commission + cooldown
# ────────────────────────────────────────────────────────────────────────────

COMMISSION    = 0.03    # per cycle (internal: bot account → broker account)
COOLDOWN_SECS = 15      # 15 seconds between cycles

# Max time to wait for an SL to fire after opening BUY+SELL straddle.
# If price is flat and neither SL fires within this window, both legs
# close and a fresh cycle starts after normal cooldown. Default: 5 min.
MAX_STRADDLE_WAIT_SECS = int(os.getenv('MAX_STRADDLE_WAIT', '1800'))

# ────────────────────────────────────────────────────────────────────────────
# Reversal detection
# ────────────────────────────────────────────────────────────────────────────

REVERSAL_WINDOW_S  = 0.4    # seconds to observe direction after SL fires
REVERSAL_TOLERANCE = 0.05   # $0.05 back toward entry = reversal (gold)
GUARDIAN_TOLERANCE = 0.00   # Guardian fires when price returns to exact SL level (0 extra)
GUARDIAN_SL_BUFFER = 0.08   # server-side SL only: buffer above SL to prevent instant trigger
                             # (MT5 SL for SELL fires on ASK; ask is already ~half-spread above mid,
                             #  so we add half-spread+margin so SL doesn't fire the moment we open)

# BTC reversal tolerances (larger because price moves are larger in $ terms)
BTC_REVERSAL_TOLERANCE = float(os.getenv('BTC_REVERSAL_TOL', '5.00'))
BTC_GUARDIAN_TOLERANCE = float(os.getenv('BTC_GUARDIAN_TOL', '10.00'))

# ────────────────────────────────────────────────────────────────────────────
# Scan interval + lot tiers
# ────────────────────────────────────────────────────────────────────────────

SCAN_INTERVAL = 0.1     # 100ms between price checks — faster reversal detection

# Early hedge: open Tickmill position this many pts BEFORE internal SL fires.
# When price is within threshold of a SL AND OB+velocity confirm the direction,
# the hedge opens immediately — capturing the remaining movement as bonus profit.
# The early hedge SL sits 1.5× threshold in the wrong direction as a safety net.
EARLY_HEDGE_THRESHOLD = float(os.getenv('EARLY_HEDGE_THRESHOLD', '0.20'))

LOT_TIERS = [
    {'min':    0, 'lot': 0.01},
    {'min':  200, 'lot': 0.02},
    {'min':  500, 'lot': 0.03},
    {'min': 1000, 'lot': 0.04},
    {'min': 2000, 'lot': 0.05},
    {'min': 5000, 'lot': 0.10},
]

# Set FIXED_LOT=0.01 in .env to override the balance tiers above.
# When set > 0, every cycle uses exactly this lot regardless of internal balance.
# Leave unset (or 0) to use the automatic tier scaling above.
FIXED_LOT = float(os.getenv('FIXED_LOT', '0'))

# ────────────────────────────────────────────────────────────────────────────
# Broker API + MT5 credentials
# ────────────────────────────────────────────────────────────────────────────

BROKER_PORT   = 8001
BROKER_SECRET = os.getenv('BROKER_SECRET', '')

MT5_LOGIN    = int(os.getenv('MT5_LOGIN',    '0'))
MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')
MT5_SERVER   = os.getenv('MT5_SERVER',   'Tickmill-Demo')
MT5_PATH     = os.getenv('MT5_PATH',     '')

HEDGE_MAGIC    = 20260808
LOCK_DEVIATION = 15

# ────────────────────────────────────────────────────────────────────────────
# Database
# ────────────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(__file__), 'broker_data.db')

# ────────────────────────────────────────────────────────────────────────────
# Simulation mode (weekend / offline testing)
# Add SIMULATE=true to .env to run without live MT5 or broker.
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────────
# Level Gravity filter
# When True: /open is only accepted if Level Gravity returns FIRE (clear move).
# Cycles are skipped in choppy/SKIP markets — keeps you out of bad entries.
# Set GRAVITY_FILTER=false in .env to disable (e.g. during backtesting).
# ────────────────────────────────────────────────────────────────────────────

GRAVITY_FILTER_ENABLED = os.getenv('GRAVITY_FILTER', 'true').lower() == 'true'
GRAVITY_STALE_SECS     = 120   # if gravity hasn't refreshed in >120s, bypass filter (MT5 offline)

# ────────────────────────────────────────────────────────────────────────────
# News filter (economic calendar + live headlines)
# Set NEWS_FILTER=false in .env to disable (e.g. backtesting / manual override)
# ────────────────────────────────────────────────────────────────────────────
NEWS_FILTER_ENABLED = os.getenv('NEWS_FILTER', 'true').lower() == 'true'

SIMULATE          = os.getenv('SIMULATE', 'false').lower() == 'true'
SIM_BASE_PRICE    = float(os.getenv('SIM_BASE_PRICE', '3350.00'))
SIM_ATR           = float(os.getenv('SIM_ATR',         '2.00'))
SIM_COOLDOWN_SECS = int(os.getenv('SIM_COOLDOWN_SECS', '10'))
