"""
AutoCycle Gold Scalper v2 — OB Retrace Engine
==============================================
XAUUSD M1 / M3 / M5 only.

Strategy (same as master.entry approach):
  1. Detect a strong displacement candle (body >= 1.5x ATR).
  2. The last OPPOSITE-colour candle before that displacement = Order Block (OB).
  3. Price almost always retraces to fill the OB before continuing.
  4. Enter at the OB body edge, SL just beyond OB wick, TP at displacement peak.

Sessions: London open 07:00-10:00 UTC  |  New York open 13:00-16:00 UTC
"""

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

log = logging.getLogger(__name__)

SYMBOL = 'XAUUSD'

SCALP_TF_MAP = {
    'M1': mt5.TIMEFRAME_M1,
    'M3': mt5.TIMEFRAME_M3,
    'M5': mt5.TIMEFRAME_M5,
}

# ── Session windows (UTC) ────────────────────────────────────────────────────
# Nigerian time = UTC+1, so midnight NGT = 23:00 UTC and 1am NGT = 00:00 UTC.
# The Asian/Sydney session starts around 22:00 UTC and produces real gold moves.
# Added so the system catches the late-night moves the user observes.
SESSIONS = [
    (2, 22),   # 03:00-23:00 NGT (02:00-22:00 UTC) — full gold trading day
               # Gold moves from Asian pre-market straight through NY close.
               # Only 22:00-02:00 UTC (23:00-03:00 NGT) is truly dead.
]

# ── Algorithm knobs ──────────────────────────────────────────────────────────
# Displacement strength required per TF.
# M5 needs a bigger candle because M5 ATR during Asian session is smaller,
# meaning weak M5 candles previously created OBs that didn't hold.
DISPLACEMENT_ATR_MIN = {
    'M1': 1.5,   # 1.5x ATR body — reasonable for fast M1 moves
    'M3': 1.7,   # slightly stronger required
    'M5': 2.0,   # strong candle required — ensures M5 OB is genuinely institutional
}

OB_LOOKBACK         = 5     # look up to 5 candles back for the OB candle
SL_BUFFER_ATR       = 0.10  # SL sits 0.10x ATR beyond OB wick extreme
MIN_RR              = 1.5   # minimum reward:risk to fire a signal
SCAN_LOOKBACK       = 30    # scan the last 30 candles for setups (30 min on M1)
CANDLES_TO_FETCH    = 120   # total candles fetched per call

# How close price must be to the OB body edge to trigger FIRE.
# Wider buffer = fires sooner (price approaching OB, not yet inside it).
# M1 uses 0.50xATR so the signal locks when price is near the zone —
# user can set the limit order before price actually touches entry.
# M5 is tighter because M5 moves are slower and we have time to wait.
OB_ENTRY_BUFFER_ATR = {
    'M1': 0.50,   # fire when price is within 0.5xATR of OB body edge
    'M3': 0.35,
    'M5': 0.20,
}

# Require liquidity sweep for FIRE on OB retrace signals.
# A swept OB = institutions grabbed stop-loss orders before reversing — the
# highest probability SMC pattern (~70%+ win rate).
# A non-swept OB is just a random candle — probability ~40%.
# With this ON: non-swept OBs show as WATCH (user can still set limit orders)
# but only swept OBs auto-lock as FIRE.
REQUIRE_SWEEP_FOR_FIRE = True

# Maximum SL size (in ATR multiples) per TF.
# M1 OBs with huge SL produce far-away TPs that take too long to hit.
# Only trade tight M1 OBs: SL ≤ 2.0xATR → TP ≤ 3.0xATR (~$9-15 max on gold M1)
# M5 has no max — larger structure is expected on higher TF.
MAX_SL_ATR = {
    'M1': 2.0,    # SL > 2xATR on M1 → skip, too wide for quick scalp
    'M3': 3.0,
    'M5': 999,    # no upper limit on M5
}

# Distance filters — separate thresholds for FIRE vs WATCH.
#
# FIRE: tight — only auto-lock when price has nearly reached the OB.
# WATCH: wide  — show the setup even when price hasn't retraced yet so
#                users can set a limit order and catch the move.
#
# Problem we're solving: a big NY open drop creates an OB that's $12+ away.
# The old single filter returned SKIP for the whole thing, so no WATCH card
# showed at all. Price then retraced into that OB and we missed the trade.
MAX_FIRE_DISTANCE_ATR = {
    'M1': 3.0,   # e.g. ATR=$3 → max $9 for FIRE
    'M3': 2.5,   # e.g. ATR=$5 → max $12.50 for FIRE
    'M5': 2.0,   # e.g. ATR=$8 → max $16 for FIRE
}
MAX_WATCH_DISTANCE_ATR = {
    'M1': 8.0,   # show WATCH even when retrace hasn't started — catches big NY moves
    'M3': 6.0,
    'M5': 4.0,
}

# Maximum age (in candles) for a valid OB. OBs older than this on their TF
# are likely stale — price has moved far away and may never return cleanly.
# M1: 20 candles = 20 minutes. Enough to catch setups during slow sessions.
MAX_OB_AGE_CANDLES = {
    'M1': 20,
    'M3': 15,
    'M5': 12,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _in_session() -> bool:
    """True during any session window — used for WATCH detection."""
    hour = datetime.now(timezone.utc).hour
    return any(s <= hour < e for s, e in SESSIONS)


def _in_fire_session() -> bool:
    """
    True only when it is safe to lock a FIRE signal.

    Asian  00:00-04:00 UTC — full window (slow, clean moves)
    London 07:00-10:00 UTC — full window (fast, reliable)
    NY     13:05-16:00 UTC — skip first 5 min (opening whipsaw trap)
    Sydney 22:00-00:00 UTC — full window (catches late NGT moves)
    """
    now  = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    # Active: 02:00-22:00 UTC (03:00-23:00 NGT)
    if h < 2 or h >= 22:
        return False         # Dead zone — 23:00-03:00 NGT, gold barely moves
    if h == 13 and m < 5:
        return False         # NY open first 5 min — avoid spike whipsaw
    return True


_candle_cache: dict = {}   # {tf_str: (fetched_at, DataFrame)}
_CANDLE_CACHE_TTL = {      # how long (seconds) to reuse cached candles per TF
    'M1': 5,   # M1 structure changes every 60s; recheck structure every 5s
    'M3': 8,   # M3 candle is 3 min; 8s recheck is plenty
    'M5': 12,  # M5 candle is 5 min; 12s recheck is plenty
}

# Track which displacement candle was last processed per TF.
# Displacement FIRE only fires ONCE per candle. Once a candle triggers, we
# mark its timestamp here. Subsequent scans of the same candle return None,
# which lets the scanner fall through to the OB retrace path instead.
# Resets automatically when a new candle closes (different timestamp).
_last_disp_candle: dict = {}   # {tf_str: candle_time}

# Displacement FIRE threshold — body must be this many × ATR to qualify.
# Set at 1.6× for M1 — strong enough to be institutional, low enough to
# actually trigger during normal London/NY sessions without needing $6+ candles.
DISP_FIRE_ATR_MIN = {
    'M1': 1.6,
    'M3': 1.8,
    'M5': 2.0,
}


def _m5_trend() -> str:
    """
    Direction bias from pure price structure — ZERO lag.

    Method (no indicators, no averaging):
      1. Take the range established by M1 candles from 10-20 minutes ago
         (candles -20 to -10 from now). This is the 'reference range'.
      2. If current M1 close > that range's high  → price has broken out UP   → bullish
      3. If current M1 close < that range's low   → price has broken down      → bearish
      4. If price is still inside the range       → market is ranging          → neutral

    Why this beats EMA21 on M5:
      - EMA21 on M5 weighs 21 candles × 5 min = 105 minutes of history.
        By the time it confirms a reversal, you've missed half the move.
      - This reads a 10-minute reference window. If price breaks that range,
        direction changes ARE detected within the next M1 candle close (1 min).

    Tight range guard: if the reference range is < 0.5×ATR, the market is
    in micro-consolidation — too noisy to read direction. Returns neutral.
    """
    try:
        df = _get_candles('M1')
        if df is None or len(df) < 25:
            return 'neutral'

        current_close = float(df['close'].iloc[-1])

        # Reference range: M1 candles from 20 to 10 bars ago (10-20 min ago)
        ref = df.iloc[-20:-10]
        ref_high = float(ref['high'].max())
        ref_low  = float(ref['low'].min())

        # Only read direction when the reference range is meaningful
        atr = _atr(df)
        if (ref_high - ref_low) < atr * 0.5:
            return 'neutral'   # range too tight — market ranging, no direction

        if current_close > ref_high:
            return 'bullish'   # price broke above recent range = bullish structure
        if current_close < ref_low:
            return 'bearish'   # price broke below recent range = bearish structure
        return 'neutral'       # price still inside range — wait for break
    except Exception:
        return 'neutral'


def _check_displacement_fire(df: pd.DataFrame, atr: float, tf_str: str) -> dict | None:
    """
    Check if the most recently CLOSED candle is a strong displacement that
    qualifies for an immediate FIRE entry — no retrace wait needed.

    This catches strong institutional moves that never pull back to an OB.
    Entry is at the CLOSE of the displacement candle itself.
    SL is behind the candle's wick. TP is 1.5× the risk distance.

    Requirements:
      - Candle body >= DISP_FIRE_ATR_MIN × ATR (strong, not noise)
      - Index -2 (last confirmed closed candle; -1 may still be forming)
      - Returns None if conditions not met
    """
    try:
        if df is None or len(df) < 3:
            return None

        # Use index -2: the last CLOSED candle. Index -1 may be mid-formation.
        c = df.iloc[-2]

        # --- Candle-once guard ---
        # A displacement candle is only fired ONCE. The M1 scanner runs every
        # second but the candle stays as iloc[-2] for up to 60s (until the next
        # candle closes). Without this guard the same candle fires 60×/min and
        # blocks the OB retrace path from running. After the first fire we mark
        # the candle time; subsequent calls for the same candle return None and
        # let the scanner fall through to _find_best_setup instead.
        candle_time = c['time']
        if _last_disp_candle.get(tf_str) == candle_time:
            return None   # already fired for this candle — wait for next one

        body = abs(float(c['close']) - float(c['open']))
        min_body = atr * DISP_FIRE_ATR_MIN.get(tf_str, 2.0)

        if body < min_body:
            return None

        direction = 'bullish' if float(c['close']) > float(c['open']) else 'bearish'
        entry     = round(float(c['close']), 2)

        if direction == 'bullish':
            sl = round(float(c['low']) - atr * SL_BUFFER_ATR, 2)
            if sl >= entry:
                sl = round(entry - atr * 0.25, 2)
        else:
            sl = round(float(c['high']) + atr * SL_BUFFER_ATR, 2)
            if sl <= entry:
                sl = round(entry + atr * 0.25, 2)

        risk = abs(entry - sl)
        if risk <= 0 or risk < atr * 0.20:
            return None

        # Cap risk — same logic as OB: SL too wide = TP too far for a scalp
        max_sl = atr * MAX_SL_ATR.get(tf_str, 999)
        if risk > max_sl:
            return None

        if direction == 'bullish':
            tp = round(entry + risk * 1.5, 2)
        else:
            tp = round(entry - risk * 1.5, 2)

        # Mark this candle as processed — future scans of the same candle return None
        _last_disp_candle[tf_str] = candle_time

        return {
            'direction':       direction,
            'entry':           entry,
            'sl':              sl,
            'tp':              tp,
            'rr':              1.5,
            'disp_body':       round(body, 2),
            'disp_atr_mult':   round(body / atr, 2),
            'candles_ago':     1,
            'swept_liquidity': False,
            'signal_type':     'displacement',
            'ob': {
                'body_high': round(max(float(c['open']), float(c['close'])), 2),
                'body_low':  round(min(float(c['open']), float(c['close'])), 2),
                'wick_high': round(float(c['high']), 2),
                'wick_low':  round(float(c['low']), 2),
                'mid':       round((float(c['open']) + float(c['close'])) / 2, 2),
            },
        }
    except Exception as exc:
        log.debug(f"Displacement fire check error: {exc}")
        return None


def _get_candles(tf_str: str) -> pd.DataFrame:
    import time as _time
    now = _time.time()
    cached = _candle_cache.get(tf_str)
    ttl    = _CANDLE_CACHE_TTL.get(tf_str, 10)
    if cached and (now - cached[0]) < ttl:
        return cached[1]   # return cached DataFrame — structure hasn't changed

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    tf    = SCALP_TF_MAP.get(tf_str, mt5.TIMEFRAME_M5)
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, CANDLES_TO_FETCH)
    if rates is None or len(rates) == 0:
        raise ValueError(f"No data for {SYMBOL} {tf_str}")
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df[['time', 'open', 'high', 'low', 'close']].reset_index(drop=True)
    _candle_cache[tf_str] = (now, df)
    return df


def _current_price() -> float:
    if not mt5.initialize():
        return 0.0
    tick = mt5.symbol_info_tick(SYMBOL)
    return round((tick.bid + tick.ask) / 2, 2) if tick else 0.0


# ── DXY Correlation Filter ───────────────────────────────────────────────────
# Gold has a strong inverse correlation with the US Dollar (~80% of the time).
# When the dollar is strengthening, gold typically falls — and vice versa.
# We compute dollar strength using EURUSD H1 (57.6% of the DXY basket),
# confirmed by USDJPY direction. If both agree, the signal is strong.
# This prevents the system from firing BUY on gold while the dollar is
# rallying, or SELL on gold while the dollar is weakening.

def _dxy_bias() -> str:
    """
    Returns dollar trend: 'usd_strong', 'usd_weak', or 'neutral'.

    Method:
      EURUSD H1 trend — if EURUSD dropped over last 4 H1 candles → USD strong
      USDJPY H1 trend — if USDJPY rose over last 4 H1 candles    → USD strong
      Both must agree (or one neutral) to avoid false signals.
      If they disagree → neutral (no suppression).
    """
    try:
        if not mt5.initialize():
            return 'neutral'

        def _h1_bias(symbol: str) -> str:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 8)
            if rates is None or len(rates) < 5:
                return 'neutral'
            closes = [r['close'] for r in rates]
            delta  = closes[-1] - closes[-5]
            atr    = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes))) / (len(closes)-1)
            if delta < -atr * 0.5:
                return 'down'
            elif delta > atr * 0.5:
                return 'up'
            return 'neutral'

        eur_bias = _h1_bias('EURUSD')   # EURUSD down = USD strong
        jpy_bias = _h1_bias('USDJPY')   # USDJPY up   = USD strong

        # USD strong: EURUSD falling AND/OR USDJPY rising
        usd_strong_votes = (1 if eur_bias == 'down' else 0) + (1 if jpy_bias == 'up' else 0)
        usd_weak_votes   = (1 if eur_bias == 'up'   else 0) + (1 if jpy_bias == 'down' else 0)

        if usd_strong_votes >= 2:
            return 'usd_strong'
        if usd_weak_votes >= 2:
            return 'usd_weak'
        # One vote each = conflict = neutral (don't suppress)
        return 'neutral'

    except Exception as exc:
        log.debug(f"DXY bias error: {exc}")
        return 'neutral'


SWEEP_LOOKBACK = 10  # candles to look back when checking for a swing extreme

def _has_liquidity_sweep(highs, lows, disp_i: int, direction: str) -> bool:
    """
    Detect whether the displacement candle swept a recent swing high or low
    before reversing — the signature of an INSTITUTIONAL Order Block.

    How it works:
      Bearish setup: the displacement candle's HIGH exceeded the highest high
      in the previous SWEEP_LOOKBACK candles. That means institutions briefly
      pushed price above the swing high (triggering everyone's buy-stop orders,
      taking that liquidity), then immediately reversed down. The last bullish
      candle before that reversal is where institutions placed their SELL orders
      = the OB. Price WILL retrace there to fill remaining orders.

      Bullish setup: mirror logic — displacement LOW swept the swing low,
      taking sell-side stop orders before reversing up.

    A sweep OB has much higher probability than a plain OB because the
    institutional fingerprint is clear: they manufactured the move to steal
    stops before executing their real position.
    """
    start = max(0, disp_i - SWEEP_LOOKBACK)
    if disp_i <= start:
        return False

    if direction == 'bearish':
        # Did the displacement candle's HIGH break above the recent swing high?
        prev_swing_high = float(np.max(highs[start:disp_i]))
        return float(highs[disp_i]) > prev_swing_high
    else:
        # Did the displacement candle's LOW break below the recent swing low?
        prev_swing_low = float(np.min(lows[start:disp_i]))
        return float(lows[disp_i]) < prev_swing_low


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 1.0
    return float(np.mean(tr[-period:]))


# ── Core: find the best OB setup ─────────────────────────────────────────────

def _find_best_setup(df: pd.DataFrame, atr: float, tf_str: str = 'M1') -> dict | None:
    """
    Scan the last SCAN_LOOKBACK candles for the best valid OB setup.
    Scans ALL directions freely — M5 trend is applied at FIRE time, not here.
    This ensures the scanner never misses a forming setup on either side.

    Freshness cap: OBs older than MAX_OB_AGE_CANDLES are skipped — stale
    OBs where price has already moved far away produce entries price never
    touches again.

    Priority rules (highest first):
      1. SWEEP OB — displacement swept a swing high/low (institutional fingerprint)
         → price ALWAYS retraces to these; this is "that man's" entry pattern
      2. REGULAR OB — valid displacement + OB, no sweep confirmation
         → still valid, but lower probability of retrace

    Within each tier, take the MOST RECENT (smallest candles_ago).
    A fresh sweep OB beats an even-fresher regular OB.
    """
    opens  = df['open'].values
    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values
    n      = len(df)
    min_body  = atr * DISPLACEMENT_ATR_MIN.get(tf_str, 1.5)
    sl_buf    = atr * SL_BUFFER_ATR
    max_age   = MAX_OB_AGE_CANDLES.get(tf_str, SCAN_LOOKBACK)
    best_sweep   = None   # best institutional (sweep) setup
    best_regular = None   # best plain OB setup (fallback)

    # Scan most-recent-first
    for disp_i in range(n - 2, max(n - SCAN_LOOKBACK - 1, 1), -1):
        # Age check FIRST — once we're past the freshness cap, stop entirely
        candles_ago_disp = n - 1 - disp_i
        if candles_ago_disp > max_age:
            break

        body = abs(closes[disp_i] - opens[disp_i])
        if body < min_body:
            continue

        direction = 'bullish' if closes[disp_i] > opens[disp_i] else 'bearish'

        # Find OB: last opposite-colour candle before this displacement
        ob_i = None
        for j in range(disp_i - 1, max(disp_i - OB_LOOKBACK - 1, 0), -1):
            jbody = abs(closes[j] - opens[j])
            if jbody < atr * 0.05:    # skip near-doji
                continue
            opp = ((direction == 'bullish' and closes[j] < opens[j]) or
                   (direction == 'bearish' and closes[j] > opens[j]))
            if opp:
                ob_i = j
                break

        if ob_i is None:
            continue

        # OB zone — body defines entry, wick defines SL anchor
        ob_bh = round(max(opens[ob_i], closes[ob_i]), 2)  # body high
        ob_bl = round(min(opens[ob_i], closes[ob_i]), 2)  # body low
        ob_wh = round(highs[ob_i], 2)                     # wick high
        ob_wl = round(lows[ob_i], 2)                      # wick low

        # Entry and SL first — TP is derived from actual risk distance.
        if direction == 'bullish':
            entry = ob_bh                               # top of bearish OB body
            sl    = round(ob_wl - sl_buf, 2)           # below OB wick + buffer
            if sl >= entry:
                sl = round(entry - atr * 0.25, 2)
        else:
            entry = ob_bl                               # bottom of bullish OB body
            sl    = round(ob_wh + sl_buf, 2)           # above OB wick + buffer
            if sl <= entry:
                sl = round(entry + atr * 0.25, 2)

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # Minimum SL — filter micro-OBs where SL is just the spread ($0.48 garbage)
        if risk < atr * 0.25:
            log.debug(f"OB too tight: risk={risk:.2f} < 0.25xATR={atr*0.25:.2f} — skip")
            continue

        # Maximum SL — filter over-wide OBs where TP would be too far to hit quickly.
        # On M1, SL > 2.0xATR means TP > 3.0xATR — too long for a scalp.
        max_sl = atr * MAX_SL_ATR.get(tf_str, 999)
        if risk > max_sl:
            log.debug(f"OB too wide: risk={risk:.2f} > {MAX_SL_ATR.get(tf_str,999)}xATR={max_sl:.2f} — skip")
            continue

        # TP = 1.5× the actual SL risk distance.
        # Scalper philosophy: quick proportional exit, not greedy ATR multiples.
        # This keeps TP close enough to be hit before a bounce reverses the trade.
        if direction == 'bullish':
            tp = round(entry + risk * 1.5, 2)
        else:
            tp = round(entry - risk * 1.5, 2)

        rr = 1.5  # always 1.5:1 by construction

        candles_ago   = n - 1 - disp_i
        swept         = _has_liquidity_sweep(highs, lows, disp_i, direction)

        setup = {
            'direction':       direction,
            'disp_idx':        disp_i,
            'disp_body':       round(body, 2),
            'disp_atr_mult':   round(body / atr, 2),
            'candles_ago':     candles_ago,
            'swept_liquidity': swept,   # True = institutional OB (sweep confirmed)
            'ob': {
                'body_high': ob_bh,
                'body_low':  ob_bl,
                'wick_high': ob_wh,
                'wick_low':  ob_wl,
                'mid':       round((ob_bh + ob_bl) / 2, 2),
            },
            'entry': round(entry, 2),
            'sl':    round(sl, 2),
            'tp':    round(tp, 2),
            'rr':    rr,
        }

        # Track sweep OBs and regular OBs separately; take freshest of each tier
        if swept:
            if best_sweep is None or candles_ago < best_sweep['candles_ago']:
                best_sweep = setup
        else:
            if best_regular is None or candles_ago < best_regular['candles_ago']:
                best_regular = setup

    # Sweep OB always wins over regular OB — it's the institutional pattern
    return best_sweep if best_sweep is not None else best_regular


# ── Dual-direction helpers ────────────────────────────────────────────────────

def _find_ob_for_direction(df: pd.DataFrame, atr: float, tf_str: str, direction: str) -> dict | None:
    """
    Find the best OB setup for ONE specific direction (bullish=BUY or bearish=SELL).

    Entry uses the OB EDGE for tighter risk and better fills:
      BUY  (bullish) → entry at ob_bl (bottom of bearish OB body)
      SELL (bearish) → entry at ob_bh (top of bullish OB body)

    SL stays at the OB wick as before (ob_wl for BUY, ob_wh for SELL).
    TP is a placeholder (1.5×risk) — caller overwrites via _adaptive_tp.
    Sweep OBs preferred over plain OBs; freshest within each tier wins.
    """
    opens  = df['open'].values
    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values
    n      = len(df)

    min_body = atr * DISPLACEMENT_ATR_MIN.get(tf_str, 1.5)
    sl_buf   = atr * SL_BUFFER_ATR
    max_age  = MAX_OB_AGE_CANDLES.get(tf_str, SCAN_LOOKBACK)

    best_sweep   = None
    best_regular = None

    for disp_i in range(n - 2, max(n - SCAN_LOOKBACK - 1, 1), -1):
        candles_ago_disp = n - 1 - disp_i
        if candles_ago_disp > max_age:
            break

        body = abs(closes[disp_i] - opens[disp_i])
        if body < min_body:
            continue

        # Only accept displacement candles matching the target direction
        candle_dir = 'bullish' if closes[disp_i] > opens[disp_i] else 'bearish'
        if candle_dir != direction:
            continue

        # Find OB: last opposite-colour candle before this displacement
        ob_i = None
        for j in range(disp_i - 1, max(disp_i - OB_LOOKBACK - 1, 0), -1):
            jbody = abs(closes[j] - opens[j])
            if jbody < atr * 0.05:
                continue
            opp = ((direction == 'bullish' and closes[j] < opens[j]) or
                   (direction == 'bearish' and closes[j] > opens[j]))
            if opp:
                ob_i = j
                break

        if ob_i is None:
            continue

        ob_bh = round(max(opens[ob_i], closes[ob_i]), 2)
        ob_bl = round(min(opens[ob_i], closes[ob_i]), 2)
        ob_wh = round(highs[ob_i], 2)
        ob_wl = round(lows[ob_i], 2)

        # OB-edge entry: BUY waits for price to reach OB bottom; SELL waits for OB top
        if direction == 'bullish':
            entry = ob_bl                        # bottom of bearish OB body
            sl    = round(ob_wl - sl_buf, 2)    # below OB wick + buffer
            if sl >= entry:
                sl = round(entry - atr * 0.25, 2)
        else:
            entry = ob_bh                        # top of bullish OB body
            sl    = round(ob_wh + sl_buf, 2)    # above OB wick + buffer
            if sl <= entry:
                sl = round(entry + atr * 0.25, 2)

        risk = abs(entry - sl)
        # OB-edge entries have inherently small risk (just the wick tail + buffer).
        # Lower threshold to atr*0.05 — the sl>=entry guard above already ensures
        # a minimum atr*0.25 in degenerate cases. atr*0.20 was too strict here.
        if risk <= 0 or risk < atr * 0.05:
            continue

        max_sl = atr * MAX_SL_ATR.get(tf_str, 999)
        if risk > max_sl:
            continue

        candles_ago = n - 1 - disp_i
        swept       = _has_liquidity_sweep(highs, lows, disp_i, direction)

        # Compute real adaptive TP here so the dead-OB check in the caller
        # uses the actual market target, not a tiny 1.5×edge_risk placeholder.
        # (A $0.15 placeholder TP would falsely flag setups as dead the moment
        # price moves $0.20, even if the real TP is $3.50 away.)
        tp   = _adaptive_tp(df, direction, entry, round(sl, 2), atr)
        rr   = round(abs(tp - entry) / risk, 2) if risk > 0 else 1.5

        setup = {
            'direction':       direction,
            'disp_idx':        disp_i,
            'disp_body':       round(body, 2),
            'disp_atr_mult':   round(body / atr, 2),
            'candles_ago':     candles_ago,
            'swept_liquidity': swept,
            'ob': {
                'body_high': ob_bh,
                'body_low':  ob_bl,
                'wick_high': ob_wh,
                'wick_low':  ob_wl,
                'mid':       round((ob_bh + ob_bl) / 2, 2),
            },
            'entry': round(entry, 2),
            'sl':    round(sl, 2),
            'tp':    round(tp, 2),
            'rr':    rr,
        }

        if swept:
            if best_sweep is None or candles_ago < best_sweep['candles_ago']:
                best_sweep = setup
        else:
            if best_regular is None or candles_ago < best_regular['candles_ago']:
                best_regular = setup

    return best_sweep if best_sweep is not None else best_regular


def _score_setup(setup: dict, m5_trend: str, dxy: str) -> float:
    """
    Score a setup 0–18. Higher = better quality.

    Components:
      Freshness   (0–8): fewer candles since OB = more relevant
      Sweep       (0–4): institutional fingerprint = higher probability
      Disp size   (0–4): stronger displacement = cleaner signal
      M5 aligned  (+4 / −2): trend alignment bonus / counter-trend penalty
      DXY aligned (+2 / −2): macro filter bonus / penalty
    """
    direction = setup['direction']
    score     = 0.0

    # Freshness: 8 pts for 0 candles ago, linearly down
    score += max(0.0, 8.0 - float(setup.get('candles_ago', 0)))

    # Institutional sweep
    if setup.get('swept_liquidity'):
        score += 4.0

    # Displacement strength (capped at 4 pts)
    atr_mult = setup.get('disp_atr_mult', 1.0)
    score += min(4.0, max(0.0, atr_mult - 1.0))

    # M5 trend alignment
    if m5_trend == direction:
        score += 4.0
    elif m5_trend != 'neutral':
        score -= 2.0

    # DXY macro alignment
    if direction == 'bullish' and dxy == 'usd_weak':
        score += 2.0
    elif direction == 'bearish' and dxy == 'usd_strong':
        score += 2.0
    elif direction == 'bullish' and dxy == 'usd_strong':
        score -= 2.0
    elif direction == 'bearish' and dxy == 'usd_weak':
        score -= 2.0

    return score


def _adaptive_tp(df: pd.DataFrame, direction: str, entry: float, sl: float, atr: float) -> float:
    """
    Find the nearest REAL market target for TP.

    Scans (in priority order):
      1. Recent swing highs/lows (last 60 candles)  ← structural levels
      2. Round numbers ($5 increments for XAUUSD)   ← psychological magnets
      3. Fallback: max(1.5×risk, 0.5×ATR)           ← safety net

    Rules:
      - Target must satisfy MIN_RR (1.5:1 minimum)
      - Target must be within 3×ATR (reachable by a scalp)
    """
    highs = df['high'].values
    lows  = df['low'].values
    n     = len(df)

    risk        = abs(entry - sl)
    min_tp_dist = max(risk * MIN_RR, atr * 0.20)   # at least 1.5:1 RR
    max_tp_dist = atr * 3.0                          # scalp cap: 3×ATR

    targets = []

    # 1. Swing highs/lows (pivot detection)
    lookback = min(60, n - 2)
    for i in range(n - lookback, n - 1):
        if i <= 0 or i >= n - 1:
            continue
        if direction == 'bullish':
            if highs[i] > highs[i - 1] and highs[i] >= highs[i + 1]:
                t    = round(float(highs[i]), 2)
                dist = t - entry
                if min_tp_dist <= dist <= max_tp_dist:
                    targets.append(t)
        else:
            if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
                t    = round(float(lows[i]), 2)
                dist = entry - t
                if min_tp_dist <= dist <= max_tp_dist:
                    targets.append(t)

    # 2. Round numbers ($5 increments — gold psychological magnets)
    STEP = 5.0
    if direction == 'bullish':
        rn = (int(entry / STEP) + 1) * STEP
        while rn <= entry + max_tp_dist:
            if (rn - entry) >= min_tp_dist:
                targets.append(round(rn, 2))
            rn += STEP
    else:
        rn = int(entry / STEP) * STEP
        while rn >= entry - max_tp_dist:
            if (entry - rn) >= min_tp_dist:
                targets.append(round(rn, 2))
            rn -= STEP

    if targets:
        return min(targets) if direction == 'bullish' else max(targets)

    # 3. Fallback
    fallback_dist = max(min_tp_dist, atr * 0.5)
    if direction == 'bullish':
        return round(entry + fallback_dist, 2)
    else:
        return round(entry - fallback_dist, 2)


# ── Main entry point ──────────────────────────────────────────────────────────

def analyze_gold_scalp(tf_str: str) -> dict:
    """
    Dual-direction gold scalp analysis.

    Scans BOTH bullish (BUY) and bearish (SELL) OBs simultaneously on every call.
    Scores each candidate with _score_setup (freshness + sweep + M5 + DXY).
    FIRE always beats WATCH in priority; within the same verdict, highest score wins.
    After TP is hit the cooldown resets and the very next scan finds the next setup.

    Returns verdict:
      FIRE  — price is at the OB edge right now during a session → lock signal
      WATCH — OB found, price hasn't retraced to entry yet → show on dashboard
      SKIP  — no clean setup in either direction
    """
    df      = _get_candles(tf_str)
    atr     = _atr(df)
    live    = _current_price()
    in_sess = _in_session()

    # ── Displacement FIRE — highest priority, unchanged ───────────────────────
    # Strong displacement candle (>= 1.6×ATR body) fires immediately at candle
    # close — no OB retrace needed. M5 + DXY gates still apply.
    if _in_fire_session():
        disp = _check_displacement_fire(df, atr, tf_str)
        if disp:
            d_dir = disp['direction']
            dxy   = 'neutral'

            if tf_str in ('M1', 'M3'):
                m5_trend = _m5_trend()
                if m5_trend != 'neutral' and m5_trend != d_dir:
                    log.info(f"GoldScan {tf_str}: DISP {d_dir} BLOCKED — M5 is {m5_trend}")
                    disp = None

            if disp is not None:
                dxy = _dxy_bias()
                if dxy == 'usd_strong' and d_dir == 'bullish':
                    log.debug(f"GoldScan {tf_str}: DISP BUY suppressed — USD strengthening")
                    disp = None
                elif dxy == 'usd_weak' and d_dir == 'bearish':
                    log.debug(f"GoldScan {tf_str}: DISP SELL suppressed — USD weakening")
                    disp = None

            if disp is not None:
                log.info(f"GoldScan {tf_str}: DISPLACEMENT FIRE {d_dir} at {disp['entry']} "
                         f"({disp['disp_atr_mult']}×ATR candle)")
                return {
                    'symbol':          SYMBOL,
                    'tf':              tf_str,
                    'price':           live,
                    'direction':       d_dir,
                    'verdict':         'FIRE',
                    'in_session':      True,
                    'price_at_ob':     True,
                    'entry':           disp['entry'],
                    'sl':              disp['sl'],
                    'tp':              disp['tp'],
                    'rr':              disp['rr'],
                    'tp_distance':     round(abs(disp['tp'] - disp['entry']), 2),
                    'ob':              disp['ob'],
                    'disp_atr_mult':   disp['disp_atr_mult'],
                    'candles_ago':     1,
                    'swept_liquidity': False,
                    'signal_type':     'displacement',
                    'dxy_bias':        dxy,
                    'atr':             round(atr, 2),
                }

    # ── Dual-direction OB scan ────────────────────────────────────────────────
    # Pre-compute shared filters once (both directions use the same values)
    dxy      = _dxy_bias()
    m5_trend = _m5_trend() if tf_str in ('M1', 'M3') else 'neutral'
    fire_ok  = _in_fire_session()
    buf      = atr * OB_ENTRY_BUFFER_ATR.get(tf_str, 0.20)

    bull_setup = _find_ob_for_direction(df, atr, tf_str, 'bullish')
    bear_setup = _find_ob_for_direction(df, atr, tf_str, 'bearish')

    _SKIP = {
        'symbol': SYMBOL, 'tf': tf_str, 'price': live, 'verdict': 'SKIP',
        'in_session': in_sess, 'direction': 'none',
        'entry': None, 'sl': None, 'tp': None, 'rr': None,
        'atr': round(atr, 2), 'ob': None, 'dxy_bias': dxy,
    }

    if not bull_setup and not bear_setup:
        log.info(f"GoldScan {tf_str}: no displacement candle >= {DISPLACEMENT_ATR_MIN.get(tf_str,1.5)}×ATR "
                 f"in last {MAX_OB_AGE_CANDLES.get(tf_str,20)} candles — SKIP (live={live} atr={round(atr,2)})")
        return {**_SKIP, 'skip_reason': f'no OB in last {MAX_OB_AGE_CANDLES.get(tf_str,20)} candles'}

    candidates = []

    for direction, setup in [('bullish', bull_setup), ('bearish', bear_setup)]:
        if setup is None:
            continue

        # ── DXY suppression ──────────────────────────────────────────────────
        if dxy == 'usd_strong' and direction == 'bullish':
            log.debug(f"GoldScan {tf_str}: BUY suppressed — USD strengthening")
            continue
        if dxy == 'usd_weak' and direction == 'bearish':
            log.debug(f"GoldScan {tf_str}: SELL suppressed — USD weakening")
            continue

        # ── Dead OB: live price already past TP ─────────────────────────────
        if live:
            ob_dead = ((direction == 'bearish' and live < setup['tp']) or
                       (direction == 'bullish' and live > setup['tp']))
            if ob_dead:
                log.info(f"GoldScan {tf_str}: {direction} OB dead (live={live} past TP={setup['tp']}) — skip")
                continue

        # ── Distance filter ──────────────────────────────────────────────────
        force_watch = False
        if live:
            dist           = abs(setup['entry'] - live)
            max_watch_dist = atr * MAX_WATCH_DISTANCE_ATR.get(tf_str, 6.0)
            max_fire_dist  = atr * MAX_FIRE_DISTANCE_ATR.get(tf_str, 3.0)
            if dist > max_watch_dist:
                log.info(f"GoldScan {tf_str}: {direction} OB too far ({dist:.2f} > {max_watch_dist:.2f}) — skip")
                continue
            if dist > max_fire_dist:
                force_watch = True

        # ── Sweep requirement ────────────────────────────────────────────────
        swept = setup.get('swept_liquidity', False)
        if REQUIRE_SWEEP_FOR_FIRE and not swept:
            force_watch = True

        # ── At-OB detection (directional: checks the entry edge) ─────────────
        # BUY entry = ob_bl → fire when price reaches OB zone (entered from top)
        # SELL entry = ob_bh → fire when price reaches OB zone (entered from bottom)
        if live:
            if direction == 'bullish':
                at_ob = live <= (setup['ob']['body_high'] + buf)
            else:
                at_ob = live >= (setup['ob']['body_low'] - buf)
        else:
            at_ob = False

        # ── M5 trend gate — blocks FIRE only, not WATCH ──────────────────────
        m5_aligned = True
        if tf_str in ('M1', 'M3') and at_ob and fire_ok and not force_watch:
            if m5_trend != 'neutral' and m5_trend != direction:
                m5_aligned = False
                log.debug(f"GoldScan {tf_str}: {direction} at OB — M5={m5_trend}, holding WATCH")

        verdict = 'FIRE' if (at_ob and fire_ok and not force_watch and m5_aligned) else 'WATCH'
        score   = _score_setup(setup, m5_trend, dxy)

        candidates.append({
            'direction': direction,
            'setup':     setup,
            'verdict':   verdict,
            'score':     score,
            'at_ob':     at_ob,
        })

    if not candidates:
        log.info(f"GoldScan {tf_str}: OBs found but all filtered (dxy={dxy} m5={m5_trend}) — SKIP")
        return {**_SKIP, 'skip_reason': f'OBs filtered — dxy={dxy} m5={m5_trend}'}

    # FIRE beats WATCH; within same verdict, highest score wins
    candidates.sort(key=lambda c: (1 if c['verdict'] == 'FIRE' else 0, c['score']), reverse=True)
    winner = candidates[0]

    setup     = winner['setup']
    direction = winner['direction']
    verdict   = winner['verdict']

    log.info(f"GoldScan {tf_str}: {verdict} {direction} | entry={setup['entry']} "
             f"sl={setup['sl']} tp={setup['tp']} rr={setup['rr']} "
             f"score={winner['score']:.1f} swept={setup.get('swept_liquidity', False)} "
             f"age={setup['candles_ago']}c m5={m5_trend} dxy={dxy}")

    return {
        'symbol':          SYMBOL,
        'tf':              tf_str,
        'price':           live,
        'direction':       direction,
        'verdict':         verdict,
        'in_session':      in_sess,
        'price_at_ob':     winner['at_ob'],
        'entry':           setup['entry'],
        'sl':              setup['sl'],
        'tp':              setup['tp'],
        'rr':              setup['rr'],
        'tp_distance':     round(abs(setup['tp'] - setup['entry']), 2),
        'ob':              setup['ob'],
        'disp_atr_mult':   setup['disp_atr_mult'],
        'candles_ago':     setup['candles_ago'],
        'swept_liquidity': setup.get('swept_liquidity', False),
        'signal_type':     setup.get('signal_type', 'ob_retrace'),
        'dxy_bias':        dxy,
        'atr':             round(atr, 2),
    }
