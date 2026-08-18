"""
Level Gravity Scalper — XAUUSD
================================
Gold is always between two price levels. It gravitates toward the nearest one.

v2 — fixes for XAUUSD at $4000+ with $0.40–$0.50 spread:
  • LEVEL_INCREMENT = 5.0  ($5 levels: $4050, $4055, $4060…)
    $1 levels were too tight — TP was smaller than the spread.
  • SL is ATR-based (1.5× M1 ATR), not level-based.
    Level-based SL with $5 increments gave 1:0.4 RR. ATR SL gives 1.2:1+.
  • Direction = momentum only (bullish→BUY to upper, bearish→SELL to lower).
    No longer skips when nearest level contradicts momentum.

Result at live=$4051.74, spread=$0.45, ATR≈$0.80:
  SELL → tp=$4050, sl=$4052.72, tp_dist=$1.52, rr=1.27 → FIRE ✓
"""

import os
import time
import logging
import math
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

SYMBOL          = os.getenv('MT5_SYMBOL', 'XAUUSD+')
VERSION         = 'v3.2 · boot-aware trend gate'  # bump this each time logic changes

# ── Level config ──────────────────────────────────────────────────────────────
LEVEL_INCREMENT = 5.0     # $5 gravity levels — matches real order clusters on Gold
                           # at $4000+ prices. Use 2.0 if Gold drops back to $2000s.

# ── Entry filter ──────────────────────────────────────────────────────────────
MIN_DIST_TO_TP  = 0.20    # Must be at least $0.20 from TP level — allows entries closer to level

# ── SL config (ATR-based) ─────────────────────────────────────────────────────
SL_ATR_MULT     = 2.0     # SL = entry ± 2.0 × M1 ATR
SL_MIN          = 0.70    # SL floor lowered to $0.70 — lets SL shrink with ATR in quiet markets
SL_MAX          = 3.00    # SL never wider than $3.00 (keeps RR sensible)

# ── Quality filters ───────────────────────────────────────────────────────────
MIN_RR          = 0.10    # Straddle broker: RR check not applicable — any clear move qualifies.
                           # Small wins compound: 40+ trades/night at $0.50–$0.80 TP adds up.
SPREAD_MARGIN   = 0.08    # Reduced from 0.15 — TP just needs to clear spread by $0.08

# ── Adaptive TP ───────────────────────────────────────────────────────────────
ATR_TP_MULT     = 1.5     # TP = min($5 level distance, 1.5×ATR)
                           # Tighter cap → TP hits faster, frees slot for next signal.
                           # More signals hitting TP compounds faster than fewer large wins.
                           # Quiet/Asian session: small ATR → tighter TP (e.g. $1.10)
                           # Active London/NY:    large ATR → wider TP  (e.g. $2.25)
                           # ATR does the work — no session filter needed.

# ── Momentum (HMA — Hull Moving Average, near-zero lag) ───────────────────────
HMA_FAST        = 5     # was EMA5  — HMA5  reacts in 1–2 candles vs 5+ for EMA
HMA_SLOW        = 20    # was EMA20 — HMA20 reacts in 4–5 candles vs 20+ for EMA
ATR_PERIOD      = 14
CANDLES         = 50

# ── Range candles (info display only) ────────────────────────────────────────
RANGE_CANDLES   = 20
RANGE_EXTREME   = 0.30

# ── Candle cache ──────────────────────────────────────────────────────────────
_candle_cache: dict = {}
_CACHE_TTL          = 5    # seconds

# ── Spike/reversal filter ─────────────────────────────────────────────────────
# After a news spike (>$3 net move across 3 M1 candles), the chasing direction
# is blocked for 4 minutes. The reversal direction can still fire naturally.
# No trades are forced — signals must still pass all normal checks.
SPIKE_THRESHOLD = 3.0   # $3 net move in 3 candles = news explosion
SPIKE_BLOCK_SEC = 0     # straddle: spikes are GOOD — fast move = SL fires quickly, TP follows
_spike_state: dict = {} # {tf_str: {'direction': 'UP'|'DOWN', 'ts': float}}

# ── 3-State Trend Gate ────────────────────────────────────────────────────────
# NORMAL → 5-vote fires freely (reversals + ranging)
# TREND  → 2+ consecutive $5 level breaks same direction detected.
#           Only with-trend signals pass. Counter-trend signals blocked.
# WARN   → Exhaustion signals fire (candles shrinking, close ratio shifting,
#           HMA slope flattening). All signals paused ~60s. Telegram alert sent.
#           After 60s → back to NORMAL so 5-vote catches the reversal cleanly.
_trend_state: dict = {}
# {tf_str: {
#   'mode':           'NORMAL' | 'TREND' | 'WARN',
#   'trend_dir':      'UP' | 'DOWN' | None,
#   'break_counter':  int  (-3 = strong DOWN, 0 = neutral, +3 = strong UP),
#   'last_level_low': float | None,
#   'warn_ts':        float,   # unix ts when WARN was entered
#   'warn_alerted':   bool,    # True once Telegram alert has been sent this WARN period
# }}

# ── 5-Vote Smart Direction System ─────────────────────────────────────────────
# Always fires BUY or SELL — never blocks signals, just picks the right direction.
# 5 independent lenses vote on direction. Majority (3+) wins.
# If a bad HMA5 flip causes a wrong vote, the other 4 lenses override it.
#
# Vote 1: HMA5 cross        — fast momentum (near zero lag)
# Vote 2: HMA5 slope        — is fast HMA rising or falling?
# Vote 3: Level gravity     — near upper level → SELL; near lower level → BUY
# Vote 4: Momentum velocity — is momentum BUILDING or DECELERATING? (reversal early warning)
# Vote 5: Candle exhaustion — are candle bodies SHRINKING? (trend running out of steam)
#
# Key: votes 3, 4, 5 detect reversals BEFORE HMA5 flips.
# When price hits upper level AND momentum decelerates AND candles shrink → SELL fires
# even while HMA5 still says BUY. We get into the reversal trade EARLY, not late.

# ── Skip sentinel ─────────────────────────────────────────────────────────────
_SKIP = {'verdict': 'SKIP', 'strategy': 'GRAVITY'}


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_candles(tf_str: str) -> pd.DataFrame | None:
    """Fetch + cache candles with EMA and ATR columns."""
    import MetaTrader5 as mt5
    now = time.time()
    cached = _candle_cache.get(tf_str)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    tf_map = {
        'M1': mt5.TIMEFRAME_M1,
        'M3': mt5.TIMEFRAME_M3,
        'M5': mt5.TIMEFRAME_M5,
    }
    tf = tf_map.get(tf_str)
    if tf is None:
        return None

    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, CANDLES)
    if rates is None or len(rates) < 25:
        return None

    df = pd.DataFrame(rates)

    # HMA (Hull Moving Average) — near-zero lag, much faster response than EMA
    df['hma_fast']  = _compute_hma(df['close'], HMA_FAST)
    df['hma_slow']  = _compute_hma(df['close'], HMA_SLOW)
    df['hma_slope'] = df['hma_fast'].diff(3)

    # ATR (using high-low range as fast approximation — avoids shift() edge cases)
    df['atr'] = (df['high'] - df['low']).ewm(span=ATR_PERIOD, adjust=False).mean()

    _candle_cache[tf_str] = (now, df)
    return df


def _get_live():
    """Return (bid, ask, spread)."""
    import MetaTrader5 as mt5
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None, None, None
    spread = round(tick.ask - tick.bid, 3)
    return round(tick.bid, 3), round(tick.ask, 3), spread


def _nearest_levels(price: float):
    """
    Return (level_below, level_above) for LEVEL_INCREMENT steps.
    e.g. price=4051.74, inc=5 → (4050.0, 4055.0)
    """
    lb = math.floor(price / LEVEL_INCREMENT) * LEVEL_INCREMENT
    la = lb + LEVEL_INCREMENT
    return round(lb, 2), round(la, 2)


def _compute_hma(series: pd.Series, period: int) -> pd.Series:
    """
    Hull Moving Average — near-zero lag moving average.
    HMA(n) = WMA( 2×WMA(n/2) − WMA(n),  floor(√n) )

    vs EMA: EMA5 takes 5+ candles to react. HMA5 reacts in 1–2.
    vs EMA: EMA20 takes 20+ candles. HMA20 reacts in 4–5.
    For M1 scalping, this matters — we catch moves as they start, not after.
    """
    half  = max(period // 2, 1)
    sqrtn = max(int(period ** 0.5), 1)

    def _wma(s: pd.Series, p: int) -> pd.Series:
        weights = np.arange(1, p + 1, dtype=float)
        return s.rolling(p).apply(
            lambda x: np.dot(x, weights) / weights.sum(), raw=True
        )

    raw = 2 * _wma(series, half) - _wma(series, period)
    return _wma(raw, sqrtn)



def _range_position(df: pd.DataFrame, live: float) -> str:
    """
    Where does live price sit within the last RANGE_CANDLES candle range?
    Returns 'top', 'bottom', or 'middle'.

    'top'    → price in top 20%    → BUY needs 3/3 (exhaustion guard)
    'bottom' → price in bottom 20% → SELL needs 3/3 (exhaustion guard)
    'middle' → middle 60%          → normal 2/3 rule applies
    """
    recent = df.tail(RANGE_CANDLES)
    r_high = float(recent['high'].max())
    r_low  = float(recent['low'].min())
    r_size = r_high - r_low

    if r_size < 0.10:   # flat/micro range — no filter
        return 'middle'

    if live >= r_high - RANGE_EXTREME * r_size:
        return 'top'
    if live <= r_low  + RANGE_EXTREME * r_size:
        return 'bottom'
    return 'middle'


def _get_ob_bias_quick(sym: str) -> str | None:
    """
    Lightweight order-book snapshot — returns 'BUY', 'SELL', 'NEUTRAL', or None.
    Called only when candle exhaustion (v5) fires, to confirm or ignore it.
    bid/ask volume ratio ≥2.0 = buyer dominance (BUY), ≤0.5 = seller dominance (SELL).
    """
    try:
        import MetaTrader5 as mt5
        mt5.market_book_add(sym)
        time.sleep(0.05)
        book_data = mt5.market_book_get(sym)
        mt5.market_book_release(sym)
        if not book_data:
            return None
        bid_vol = sum(item.volume for item in book_data if item.type == mt5.BOOK_TYPE_BUY)
        ask_vol = sum(item.volume for item in book_data if item.type == mt5.BOOK_TYPE_SELL)
        if ask_vol == 0:
            return 'BUY'
        if bid_vol == 0:
            return 'SELL'
        ratio = bid_vol / ask_vol
        if ratio >= 2.0:
            return 'BUY'
        if ratio <= 0.5:
            return 'SELL'
        return 'NEUTRAL'
    except Exception:
        return None


def _smart_direction(df: pd.DataFrame, level_low: float, level_high: float,
                     live: float, atr: float) -> tuple:
    """
    5-Vote Smart Direction Selector — always returns 'bullish' or 'bearish'.
    Never blocks signals. 3+ votes out of 5 wins. Tiebreaker = HMA cross (v1).

    ── VOTE 1: HMA5 cross ─────────────────────────────────────────────────────
    HMA5 > HMA20 = bull. Near-zero lag. This is the baseline.

    ── VOTE 2: HMA5 slope direction ────────────────────────────────────────────
    Is the fast HMA itself rising? Adds confirmation when slope agrees with cross.
    When slope DISAGREES with cross (decelerating) → early warning.

    ── VOTE 3: Level gravity position ──────────────────────────────────────────
    Where in the $5 range is price right now?
    • Top 35% of range → near upper level → gravity says SELL (rejection expected)
    • Bottom 35% of range → near lower level → gravity says BUY (bounce expected)
    • Middle 30% → follow HMA cross (no level pressure either way)
    This prevents buying at the top or selling at the bottom of the range.

    ── VOTE 4: Momentum velocity (REVERSAL EARLY WARNING) ──────────────────────
    Is the HMA5 slope ACCELERATING or DECELERATING?
    accel = slope[-1] - slope[-2]  (rate of change of slope)
    • accel > 0 → momentum BUILDING → continue in HMA direction (v1)
    • accel ≤ 0 → momentum FADING → vote OPPOSITE of HMA (early reversal warning)
    This fires the reversal trade BEFORE HMA5 actually flips.
    Example: HMA5 still bullish but slope slowing → v4 votes SELL early.

    ── VOTE 5: Candle body exhaustion ──────────────────────────────────────────
    Compare average body size of last 3 candles vs prior 5 candles.
    • Bodies shrinking (< 60% of prior avg) = trend losing steam → vote for REVERSAL
    • Bodies stable or growing = trend healthy → follow HMA cross (v1)
    Works together with v3 + v4: all three detect reversals from different angles.

    Returns: ('bullish'|'bearish', bull_vote_count 0-5)
    bull_vote_count is used by app.py for the same-direction 3/3 re-entry rule.
    """
    n    = len(df)
    last = df.iloc[-1]

    # ── Vote 1: HMA5 cross ────────────────────────────────────────────────────
    v1_bull = bool(last['hma_fast'] > last['hma_slow'])

    # ── Vote 2: HMA5 slope direction ─────────────────────────────────────────
    v2_bull = bool(last['hma_slope'] > 0)

    # ── Vote 3: Level gravity position ───────────────────────────────────────
    zone = level_high - level_low   # always 5.0
    pos  = (live - level_low) / zone  # 0.0 = at lower level, 1.0 = at upper level
    if pos >= 0.65:       # top 35% of range → near upper level → gravity = SELL
        v3_bull = False
    elif pos <= 0.35:     # bottom 35% of range → near lower level → gravity = BUY
        v3_bull = True
    else:                 # middle 30% — no level pressure, follow HMA
        v3_bull = v1_bull

    # ── Vote 4: Momentum velocity (reversal early warning) ───────────────────
    if n >= 3:
        slope_now  = float(df['hma_slope'].iloc[-1])
        slope_prev = float(df['hma_slope'].iloc[-2])
        accel      = slope_now - slope_prev
        # Positive accel = momentum building → continue current HMA direction
        # Negative/zero accel = momentum fading → vote for reversal (opposite of HMA)
        v4_bull = True if accel > 0 else (not v1_bull)
    else:
        v4_bull = v1_bull

    # ── Vote 5: Candle body exhaustion ───────────────────────────────────────
    v5_fired = False   # True when exhaustion signal is active (used for OB confirmation below)
    if n >= 9:
        def body(i: int) -> float:
            return abs(float(df['close'].iloc[i]) - float(df['open'].iloc[i]))
        recent_avg = (body(-1) + body(-2) + body(-3)) / 3
        prior_avg  = (body(-4) + body(-5) + body(-6) + body(-7) + body(-8)) / 5
        # Candles shrinking significantly: trend running out of steam → reversal vote
        if prior_avg > 0 and recent_avg < prior_avg * 0.60:
            v5_bull  = not v1_bull   # vote for reversal (opposite of current HMA)
            v5_fired = True          # flag: exhaustion is active, OB check will run
        else:
            v5_bull = v1_bull       # trend still healthy → follow HMA
    else:
        v5_bull = v1_bull

    # ── Majority vote ─────────────────────────────────────────────────────────
    bulls = sum([v1_bull, v2_bull, v3_bull, v4_bull, v5_bull])
    # 3+ = clear majority. On exact tie (shouldn't happen with 5 votes but guard anyway)
    direction = 'bullish' if bulls >= 3 else 'bearish'

    # Score = votes FOR the winning direction (not raw bull count).
    # Without this fix a 5/5 SELL signal returns score=0 (bull votes)
    # and gets blocked by the engine's score < 3 threshold.
    score_for_direction = bulls if direction == 'bullish' else (5 - bulls)

    return direction, int(score_for_direction), v5_fired


def _exhaustion_score(df: pd.DataFrame, is_downtrend: bool) -> int:
    """
    Measures how close the current trend is to exhaustion.
    Returns 0–3. Score ≥ 2 triggers WARN mode.

    Three independent signals — each adds 1 point:

    1. ATR contraction
       Recent 3-candle average range < 65% of 14-period ATR.
       Candles are physically getting smaller → momentum dying.

    2. Close ratio shift
       In a downtrend: last candle closes in the UPPER 55%+ of its range.
       Price tried to push lower but buyers pushed it back up → rejection forming.
       In an uptrend: last candle closes in the LOWER 45% of its range.
       Same logic inverted.

    3. HMA slope flattening
       Slope acceleration is turning AGAINST the trend direction.
       In a downtrend: accel > 0 means slope is getting less negative → slowing.
       In an uptrend:  accel < 0 means slope is getting less positive → slowing.
       This is early — fires 1–2 candles before HMA actually flips.
    """
    score = 0
    n     = len(df)

    # ── 1. ATR contraction ────────────────────────────────────────────────────
    if n >= 3:
        recent_ranges = [
            float(df['high'].iloc[i]) - float(df['low'].iloc[i])
            for i in [-1, -2, -3]
        ]
        recent_avg = sum(recent_ranges) / 3
        atr_val    = float(df['atr'].iloc[-1])
        if atr_val > 0 and recent_avg < 0.65 * atr_val:
            score += 1

    # ── 2. Close ratio shift ─────────────────────────────────────────────────
    last = df.iloc[-1]
    hi   = float(last['high'])
    lo   = float(last['low'])
    cl   = float(last['close'])
    if hi > lo:
        cr = (cl - lo) / (hi - lo)
        if is_downtrend and cr > 0.55:    # closing in upper half during downtrend
            score += 1
        elif not is_downtrend and cr < 0.45:  # closing in lower half during uptrend
            score += 1

    # ── 3. HMA slope flattening ───────────────────────────────────────────────
    if n >= 3:
        slope_now  = float(df['hma_slope'].iloc[-1])
        slope_prev = float(df['hma_slope'].iloc[-2])
        accel      = slope_now - slope_prev
        if is_downtrend and accel > 0:      # slope getting less negative = slowing
            score += 1
        elif not is_downtrend and accel < 0:  # slope getting less positive = slowing
            score += 1

    return score


def _trend_vitality(df: pd.DataFrame, trend_direction: str) -> tuple:
    """
    Trend Vitality Score — is the current trend still alive or running out of energy?

    Returns (score: int 0-3, label: str).

    Three components, 1 point each:
      1. Body momentum  — are recent candle bodies still as large as before?
                          Shrinking bodies = energy leaving the trend.
      2. HMA acceleration — is slope still building in the trend direction?
                          Decelerating = momentum dying.
      3. Price progress  — is price still making new highs / new lows?
                          Stalling = trend can't push further.

    Score → label:
        3 = STRONG    → trend healthy — counter-signal needs 4/5 votes to fire
        2 = FADING    → trend weakening — counter-signal still needs 4/5 votes
        1 = WEAKENING → losing steam — standard 3/5 threshold applies
        0 = EXHAUSTED → trend spent — 5-vote reversal at 3/5 is fully trusted

    Only counter-trend signals are evaluated.  With-trend signals always pass.
    """
    n       = len(df)
    is_down = trend_direction == 'DOWN'
    score   = 0

    if n < 6:
        return 2, 'FADING'   # not enough history — neutral

    # ── 1. Body momentum ──────────────────────────────────────────────────────
    # Shrinking candle bodies = sellers/buyers exhausted
    def body(i: int) -> float:
        return abs(float(df['close'].iloc[i]) - float(df['open'].iloc[i]))

    recent_body = (body(-1) + body(-2) + body(-3)) / 3
    prior_body  = (body(-4) + body(-5) + body(-6)) / 3

    if prior_body > 0 and recent_body >= prior_body * 0.70:
        score += 1   # bodies holding — trend has fuel

    # ── 2. HMA slope acceleration ─────────────────────────────────────────────
    # Is momentum still building in the trend direction, or slowing down?
    if n >= 4 and 'hma_slope' in df.columns:
        s_prev = float(df['hma_slope'].iloc[-3])
        s_curr = float(df['hma_slope'].iloc[-1])
        if is_down:
            # Healthy downtrend: slope is negative and getting more negative
            if s_curr < 0 and s_curr <= s_prev:
                score += 1
        else:
            # Healthy uptrend: slope is positive and growing more positive
            if s_curr > 0 and s_curr >= s_prev:
                score += 1

    # ── 3. Price progress ─────────────────────────────────────────────────────
    # Is price still breaking into new territory, or has it stalled?
    if n >= 5:
        if is_down:
            recent_low = min(float(df['low'].iloc[-1]), float(df['low'].iloc[-2]))
            prior_low  = min(float(df['low'].iloc[-4]), float(df['low'].iloc[-5]))
            if recent_low < prior_low:
                score += 1   # still making new lows — downtrend alive
        else:
            recent_high = max(float(df['high'].iloc[-1]), float(df['high'].iloc[-2]))
            prior_high  = max(float(df['high'].iloc[-4]), float(df['high'].iloc[-5]))
            if recent_high > prior_high:
                score += 1   # still making new highs — uptrend alive

    labels = {3: 'STRONG', 2: 'FADING', 1: 'WEAKENING', 0: 'EXHAUSTED'}
    return score, labels[score]


def _market_structure(df: pd.DataFrame) -> str:
    """
    Detects the real trend direction from pure price behaviour.
    No moving averages. No lag. Just what candles are actually doing.

    Two independent measurements — BOTH must agree:

    1. Structure (HH+HL / LH+LL):
       Split last 12 candles into two 6-candle blocks.
       Recent block highs/lows vs prior block highs/lows.
       - Recent higher than prior → uptrend structure
       - Recent lower than prior  → downtrend structure
       - Mixed                    → no clear structure

    2. Close Ratio (buying vs selling pressure):
       For each of the last 5 candles: where did close land in the range?
       (close - low) / (high - low)
       - Average > 0.65 → candles keep closing near their highs = buyers in control
       - Average < 0.35 → candles keep closing near their lows  = sellers in control
       - 0.35–0.65     → indecision / ranging

    Returns:
        'UP'      — both structure and momentum confirm uptrend
        'DOWN'    — both structure and momentum confirm downtrend
        'RANGING' — mixed or uncertain → 5-vote fires freely, no interference
    """
    n     = len(df)
    BLOCK = 6

    if n < BLOCK * 2 + 5:
        return 'RANGING'

    # ── 1. Structure: Higher Highs + Higher Lows  /  Lower Highs + Lower Lows ─
    MIN_MOVE  = 0.50   # must move at least $0.50 to count as a real directional push

    recent_high = float(df['high'].iloc[-BLOCK:].max())
    recent_low  = float(df['low'].iloc[-BLOCK:].min())
    prior_high  = float(df['high'].iloc[-BLOCK * 2:-BLOCK].max())
    prior_low   = float(df['low'].iloc[-BLOCK * 2:-BLOCK].min())

    struct_up   = (recent_high > prior_high + MIN_MOVE and
                   recent_low  > prior_low  + MIN_MOVE)
    struct_down = (recent_high < prior_high - MIN_MOVE and
                   recent_low  < prior_low  - MIN_MOVE)

    # ── 2. Close Ratio: where did each candle close within its own range? ─────
    def _close_ratio(i: int) -> float:
        h = float(df['high'].iloc[i])
        lo = float(df['low'].iloc[i])
        c = float(df['close'].iloc[i])
        return (c - lo) / (h - lo) if h != lo else 0.5

    ratio_avg    = sum(_close_ratio(i) for i in range(-5, 0)) / 5
    momentum_up  = ratio_avg > 0.65
    momentum_dn  = ratio_avg < 0.35

    # ── 3. Both must agree ────────────────────────────────────────────────────
    if struct_up   and momentum_up:  return 'UP'
    if struct_down and momentum_dn:  return 'DOWN'
    return 'RANGING'


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def analyze_gravity_scalp(tf_str: str = 'M1') -> dict:
    """
    Level Gravity analysis — called by scanner._gravity_loop() every ~15 s.

    Logic:
      1. Get live bid/ask + ATR
      2. Find nearest $5 levels above and below
      3. 5-vote smart direction: HMA cross, slope, level position,
         momentum velocity, candle exhaustion → majority wins
      4. Always fires BUY or SELL — no blocking, no waiting
      5. BUY toward upper $5 level | SELL toward lower $5 level
      6. SL = entry ± (2.0 × ATR), clipped to [SL_MIN, SL_MAX]
      7. Validate: TP > spread + margin | RR ≥ MIN_RR

    Returns FIRE dict or SKIP dict with skip_reason.
    """

    # ── 1. Live prices ────────────────────────────────────────────────────────
    bid, ask, spread = _get_live()
    if bid is None:
        return {**_SKIP, 'skip_reason': 'no tick data from MT5'}

    live = round((bid + ask) / 2, 3)

    # ── 2. Candles + ATR ─────────────────────────────────────────────────────
    df = _get_candles(tf_str)
    if df is None:
        return {**_SKIP, 'skip_reason': 'candle fetch failed'}

    atr = float(df['atr'].iloc[-1])
    if atr <= 0:
        return {**_SKIP, 'skip_reason': f'ATR={atr:.3f} invalid'}

    # ── 3. Nearest $5 levels ──────────────────────────────────────────────────
    level_low, level_high = _nearest_levels(live)
    dist_up = round(level_high - live, 3)
    dist_dn = round(live - level_low, 3)

    # ── 3b. 3-State Trend Machine — update break counter + mode ──────────────
    ts_now   = time.time()
    ts_state = _trend_state.get(tf_str, {
        'mode':           'NORMAL',
        'trend_dir':      None,
        'break_counter':  0,
        'last_level_low': None,
        'warn_ts':        0.0,
        'warn_alerted':   False,
    })

    # Track which $5 level we're in. Each time it changes, note the direction.
    last_ll = ts_state.get('last_level_low')
    bc      = ts_state.get('break_counter', 0)

    if last_ll is None:
        # ── First run after restart — initialise from candle history ─────────
        # Without this, the system wakes up blind (bc=0, NORMAL mode) even
        # during a strong established trend. By reading the last 15 closes we
        # immediately know which way price has been travelling, so the trend
        # gate activates the moment the service starts.
        closes   = df['close'].values
        lookback = min(15, len(closes))
        lvls     = [math.floor(c / LEVEL_INCREMENT) * LEVEL_INCREMENT
                    for c in closes[-lookback:]]
        bc = 0
        for i in range(1, len(lvls)):
            if lvls[i] > lvls[i - 1]:
                bc = min(bc + 1, 3)
            elif lvls[i] < lvls[i - 1]:
                bc = max(bc - 1, -3)
        log.info(f"[GravityTrend] Boot-init from history: bc={bc} "
                 f"(last {lookback} candles, live={live})")
    else:
        if level_low < last_ll - 0.1:        # moved into a LOWER $5 level
            bc = max(bc - 1, -3)
        elif level_low > last_ll + 0.1:      # moved into a HIGHER $5 level
            bc = min(bc + 1, 3)

    ts_state['last_level_low'] = level_low
    ts_state['break_counter']  = bc

    # Mode transitions
    mode      = ts_state.get('mode', 'NORMAL')
    trend_dir = ts_state.get('trend_dir')

    if mode == 'NORMAL':
        if bc <= -2:
            mode = 'TREND';  trend_dir = 'DOWN'
            ts_state['warn_alerted'] = False
            log.info(f"[GravityTrend] TREND DOWN — bc={bc}, live={live}")
        elif bc >= 2:
            mode = 'TREND';  trend_dir = 'UP'
            ts_state['warn_alerted'] = False
            log.info(f"[GravityTrend] TREND UP — bc={bc}, live={live}")

    elif mode == 'TREND':
        is_down   = (trend_dir == 'DOWN')
        exh_score = _exhaustion_score(df, is_down)
        if exh_score >= 2:
            mode = 'WARN'
            ts_state['warn_ts']      = ts_now
            ts_state['warn_alerted'] = False
            log.info(f"[GravityTrend] WARN — exhaustion={exh_score}/3, dir={trend_dir}, live={live}")
        elif (trend_dir == 'DOWN' and bc >= 0) or (trend_dir == 'UP' and bc <= 0):
            # Break counter has fully reversed — trend has changed
            mode = 'NORMAL';  trend_dir = None
            log.info(f"[GravityTrend] TREND → NORMAL (bc flipped to {bc})")

    elif mode == 'WARN':
        warn_age = ts_now - ts_state.get('warn_ts', 0)
        if warn_age > 60:          # 60s window: 5-vote catches the reversal, then normal resumes
            mode = 'NORMAL';  trend_dir = None
            log.info(f"[GravityTrend] WARN expired → NORMAL after {int(warn_age)}s")

    ts_state['mode']      = mode
    ts_state['trend_dir'] = trend_dir
    _trend_state[tf_str]  = ts_state

    # ── 4. Smart Direction — 5-vote system ───────────────────────────────────────
    # Always picks BUY or SELL. 5 lenses vote; 3+ wins.
    # Votes 3/4/5 detect reversals BEFORE HMA5 flips — we get in early, not late.
    # When the market turns, these votes flip first and override HMA5 immediately.
    range_pos = _range_position(df, live)   # info only

    momentum, momentum_score, v5_fired = _smart_direction(df, level_low, level_high, live, atr)
    is_bull   = momentum == 'bullish'
    direction = 'BUY' if is_bull else 'SELL'
    bias      = None

    # ── 4b. Exhaustion + OB double-confirmation block ─────────────────────────
    # v5 (candle exhaustion) fired means candle bodies are shrinking — the move is
    # running out of steam. Alone, v5 can be outvoted by the 4 momentum signals.
    # But if the order book ALSO shows the opposite side dominating, that is two
    # independent real-time signals agreeing: the entry direction is wrong.
    # In that case, skip — we are about to enter into an exhaustion candle.
    if v5_fired:
        _ob = _get_ob_bias_quick(SYMBOL)
        _opposite = 'SELL' if is_bull else 'BUY'
        if _ob == _opposite:
            log.info(
                f'[GravityExhaustion] BLOCKED {direction} — exhaustion fired '
                f'+ OB={_ob} confirms {_opposite} pressure | live={live}'
            )
            return {**_SKIP,
                    'skip_reason':    (
                        f'Exhaustion+OB: candles shrinking ({direction} fading) '
                        f'+ OB={_ob} ({_opposite} dominant) — exhaustion entry blocked'
                    ),
                    'live':           live,
                    'level_high':     level_high,
                    'level_low':      level_low,
                    'momentum_score': momentum_score}

    # ── 4c. Trend Gate — apply 3-state machine result ────────────────────────
    # The 5-vote ran and gave us a direction. Now the gate decides whether it passes.
    #
    # TREND mode: only with-trend signals pass. Counter-trend = silent SKIP.
    #   Example: TREND DOWN active → 5-vote fires BUY → blocked.
    #            5-vote fires SELL → allowed through (with-trend).
    #
    # WARN mode: all signals paused. Exhaustion detected. Telegram fires once.
    #            After 60s the gate opens and 5-vote catches the reversal.
    #
    # NORMAL mode: no gate. 5-vote fires freely in both directions (as before).
    trend_warn_fire = False   # True only on the first scan of a new WARN window

    if mode == 'TREND':
        if trend_dir == 'DOWN' and is_bull:
            log.info(f"[GravityTrend] BLOCKED BUY — TREND DOWN (bc={bc}, live={live})")
            return {**_SKIP,
                    'skip_reason':  f'TREND DOWN — BUY blocked (break_counter={bc})',
                    'live':         live,
                    'level_high':   level_high,
                    'level_low':    level_low,
                    'trend_mode':   'TREND',
                    'trend_dir':    trend_dir,
                    'break_counter': bc}
        elif trend_dir == 'UP' and not is_bull:
            log.info(f"[GravityTrend] BLOCKED SELL — TREND UP (bc={bc}, live={live})")
            return {**_SKIP,
                    'skip_reason':  f'TREND UP — SELL blocked (break_counter={bc})',
                    'live':         live,
                    'level_high':   level_high,
                    'level_low':    level_low,
                    'trend_mode':   'TREND',
                    'trend_dir':    trend_dir,
                    'break_counter': bc}

    elif mode == 'WARN':
        # Fire Telegram alert only once per WARN window
        if not ts_state.get('warn_alerted', False):
            trend_warn_fire = True
            ts_state['warn_alerted'] = True
            _trend_state[tf_str]     = ts_state
        warn_remaining = max(0, int(60 - (ts_now - ts_state.get('warn_ts', 0))))
        log.info(f"[GravityTrend] WARN pause — {warn_remaining}s remaining, dir={trend_dir}")
        return {**_SKIP,
                'skip_reason':  f'WARN — trend exhausting ({warn_remaining}s), reversal window opening',
                'live':         live,
                'level_high':   level_high,
                'level_low':    level_low,
                'trend_mode':   'WARN',
                'trend_dir':    trend_dir,
                'break_counter': bc,
                'trend_warn':   trend_warn_fire}

    if is_bull:
        entry       = ask
        level_tp    = level_high
        tp_dist_raw = min(abs(level_tp - entry), ATR_TP_MULT * atr)
        tp          = round(entry + tp_dist_raw, 2)
    else:
        entry       = bid
        level_tp    = level_low
        tp_dist_raw = min(abs(level_tp - entry), ATR_TP_MULT * atr)
        tp          = round(entry - tp_dist_raw, 2)

    # ── 4d. Spike/reversal filter ─────────────────────────────────────────────
    # Detect if last 3 candles made a big directional move (news spike).
    # If so, block the chasing direction — reversal direction fires naturally.
    now_ts  = time.time()
    closes  = df['close'].values
    if len(closes) >= 4:
        net_3 = float(closes[-1] - closes[-4])  # net change over last 3 complete candles
        if net_3 > SPIKE_THRESHOLD:
            _spike_state[tf_str] = {'direction': 'UP', 'ts': now_ts}
            log.info(f"[GravitySpike] UP spike detected: +${net_3:.2f} in 3 candles — blocking BUY for {SPIKE_BLOCK_SEC}s")
        elif net_3 < -SPIKE_THRESHOLD:
            _spike_state[tf_str] = {'direction': 'DOWN', 'ts': now_ts}
            log.info(f"[GravitySpike] DOWN spike detected: -${abs(net_3):.2f} in 3 candles — blocking SELL for {SPIKE_BLOCK_SEC}s")

    spike = _spike_state.get(tf_str)
    if spike:
        age = now_ts - spike['ts']
        if age < SPIKE_BLOCK_SEC:
            remaining = int(SPIKE_BLOCK_SEC - age)
            if spike['direction'] == 'UP' and direction == 'BUY':
                return {**_SKIP,
                        'skip_reason': f'spike UP — BUY blocked ({remaining}s left, reversal SELL allowed)',
                        'live': live, 'level_high': level_high, 'level_low': level_low}
            elif spike['direction'] == 'DOWN' and direction == 'SELL':
                return {**_SKIP,
                        'skip_reason': f'spike DOWN — SELL blocked ({remaining}s left, reversal BUY allowed)',
                        'live': live, 'level_high': level_high, 'level_low': level_low}
        else:
            _spike_state.pop(tf_str, None)  # window expired — clear state

    # ── 5. Distance to TP (from actual entry price) ───────────────────────────
    tp_dist = round(tp_dist_raw, 3)

    # Guard: already AT the target level — no room to profit
    dist_to_tp_mid = dist_up if is_bull else dist_dn
    if dist_to_tp_mid < MIN_DIST_TO_TP:
        return {**_SKIP,
                'skip_reason': (f'price ${live} too close to target level ${tp:.0f} '
                                f'(dist={dist_to_tp_mid:.2f} < {MIN_DIST_TO_TP})'),
                'live': live, 'level_high': level_high, 'level_low': level_low}

    # TP profitability guard removed — straddle broker uses fixed TP_EXTRA ($0.30+),
    # not the gravity level distance. Checking level distance here blocks valid entries
    # when price sits near a $5 boundary.

    # ── 6. ATR-based SL ───────────────────────────────────────────────────────
    sl_dist_raw = min(max(atr * SL_ATR_MULT, SL_MIN), SL_MAX)

    if is_bull:
        sl = round(entry - sl_dist_raw, 2)
    else:
        sl = round(entry + sl_dist_raw, 2)

    sl_dist = round(abs(sl - entry), 3)

    # ── 7. RR check ───────────────────────────────────────────────────────────
    if sl_dist == 0:
        return {**_SKIP, 'skip_reason': 'zero SL distance'}

    rr = round(tp_dist / sl_dist, 2)
    if rr < MIN_RR:
        return {**_SKIP,
                'skip_reason': f'RR {rr:.2f} < min {MIN_RR} (tp={tp_dist:.2f} sl={sl_dist:.2f})',
                'live': live, 'level_high': level_high, 'level_low': level_low}

    # ── 8. FIRE ───────────────────────────────────────────────────────────────
    atr_tp      = round(ATR_TP_MULT * atr, 2)
    tp_capped   = tp_dist < abs(level_tp - entry) - 0.01   # True = ATR capped TP (not full level)

    # TP1 = 50% of TP distance — breakeven trigger for the bot
    if is_bull:
        tp1 = round(entry + tp_dist * 0.50, 2)
    else:
        tp1 = round(entry - tp_dist * 0.50, 2)

    return {
        'verdict':    'FIRE',
        'strategy':   'GRAVITY',
        'version':    VERSION,
        'direction':  direction,
        'entry':      round(entry, 2),
        'tp':         round(tp, 2),
        'tp1':        tp1,              # 50% of TP — bot moves SL to entry when price hits this
        'sl':         round(sl, 2),
        'rr':         rr,
        'tp_dist':    round(tp_dist, 2),
        'sl_dist':    round(sl_dist, 2),
        'atr':        round(atr, 3),
        'atr_tp':     atr_tp,
        'level_tp':   round(level_tp, 2),
        'tp_capped':  tp_capped,
        'level_high': round(level_high, 2),
        'level_low':  round(level_low, 2),
        'momentum':        momentum,
        'momentum_score':  momentum_score,   # 0-5 how many of 5 votes agreed with direction
        'trend_mode':      mode,             # NORMAL / TREND / WARN
        'trend_dir':       trend_dir,        # UP / DOWN / None
        'break_counter':   bc,               # -3 to +3 level break counter
        'bias':           bias,
        'range_pos':      range_pos,
        'spread':     round(spread, 3),
        'live':       live,
        'dist_up':    dist_up,
        'dist_dn':    dist_dn,
    }
