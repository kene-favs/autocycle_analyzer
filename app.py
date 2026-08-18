"""
Trend Analyzer — Flask Web Server
===================================
Serves the chart dashboard and exposes the /api/analyze endpoint.

HOW TO RUN
----------
    pip install flask flask-cors MetaTrader5 pandas numpy scipy
    python app.py

Then open your browser at: http://localhost:5000

REQUIREMENTS
------------
- MetaTrader5 must be open and logged into your account
- Vantage broker: symbol is XAUUSD+ (set MT5_SYMBOL env var to override)
"""

import json
import logging
import os
import time
import MetaTrader5 as mt5
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_cors import CORS
from trend_analyzer import analyze, is_active_session
import scanner

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# MT5 credentials — read from .env so the analyzer logs in automatically
_MT5_LOGIN    = int(os.getenv('MT5_LOGIN', '0'))
_MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')
_MT5_SERVER   = os.getenv('MT5_SERVER', '')

# Gold symbol — Vantage uses 'XAUUSD+'; set MT5_SYMBOL in .env to override
GOLD_SYMBOL = os.getenv('MT5_SYMBOL', 'XAUUSD+')

# Autocycle AI Broker — runs on same VPS, different port
_BROKER_URL = os.getenv('BROKER_URL', 'http://localhost:8001')


def _mt5_init() -> bool:
    """Initialize MT5 with credentials if provided, else fallback to terminal session."""
    if _MT5_LOGIN and _MT5_PASSWORD and _MT5_SERVER:
        return mt5.initialize(login=_MT5_LOGIN, password=_MT5_PASSWORD, server=_MT5_SERVER)
    return mt5.initialize()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True   # always re-read templates from disk
CORS(app)



# ── Signal Cache ────────────────────────────────────────────────────────────────
# Once a signal fires it is LOCKED until price crosses SL/TP or the TTL expires.
# This stops the entry price from changing every time you click Analyze.
_signal_cache: dict = {}

_CACHE_TTL = {          # seconds a signal stays valid before auto-expiry
    'M5':    15 * 60,   # 15 minutes
    'M15':   60 * 60,   # 1 hour
    'M30': 2 * 60 * 60, # 2 hours
    'H1':  4 * 60 * 60, # 4 hours
    'H4': 24 * 60 * 60, # 24 hours
    'D1':  7 * 24 * 60 * 60,
    'auto':  60 * 60,
}


def _current_price(symbol: str):
    """Fast MT5 tick fetch — no full candle analysis needed."""
    try:
        _mt5_init()
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            return (tick.bid + tick.ask) / 2
    except Exception:
        pass
    return None


def _cache_valid(cached: dict, symbol: str):
    """
    Returns (is_valid: bool, reason: str).
    A signal is invalidated when:
      • TTL has expired
      • Price has crossed the SL (would have stopped out)
      • Price has crossed the TP (profit taken)
      • HTF bias has FLIPPED against the signal direction since it was cached
        (e.g. M15 BUY cached when H1 was bullish — now H1 is bearish → stale)
    """
    age = time.time() - cached['ts']
    tf  = cached.get('tf', 'H1')
    if age > _CACHE_TTL.get(tf, 3600):
        return False, 'expired'

    trade     = cached['data'].get('trade', {})
    direction = cached['data'].get('pattern', {}).get('direction')
    sl        = trade.get('sl')
    tp        = trade.get('tp')

    if not direction or not sl or not tp:
        return True, 'valid'

    price = _current_price(symbol)
    if price is None:
        return True, 'valid'   # can't verify — keep cached

    if direction == 'bearish':
        if price >= sl: return False, 'sl_crossed'
        if price <= tp: return False, 'tp_hit'
    elif direction == 'bullish':
        if price <= sl: return False, 'sl_crossed'
        if price >= tp: return False, 'tp_hit'

    # NOTE: neckline check removed — sniper entries are at OB (below BOS neckline),
    # so a neckline check would always invalidate valid setups. SL covers the real risk.

    return True, 'valid'


# FBS broker symbols — NO 'm' suffix
SUGGESTED_SYMBOLS = [
    # Gold & metals
    'XAUUSD+', 'XAGUSD+',
    # Majors
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'NZDUSD', 'USDCAD',
    # Euro crosses
    'EURGBP', 'EURJPY', 'EURCHF', 'EURCAD', 'EURAUD', 'EURNZD',
    # GBP crosses
    'GBPJPY', 'GBPCHF', 'GBPCAD', 'GBPAUD', 'GBPNZD',
    # AUD crosses
    'AUDJPY', 'AUDCAD', 'AUDCHF', 'AUDNZD',
    # Other crosses
    'CADJPY', 'CADCHF', 'NZDJPY', 'NZDCAD', 'NZDCHF', 'CHFJPY',
    # Exotics
    'USDZAR', 'USDNOK', 'USDSEK', 'USDMXN', 'USDTRY', 'USDSGD',
    # Oil & commodities
    'XTIUSD', 'XBRUSD',
    # Indices
    'US30', 'US500', 'NAS100', 'UK100', 'GER40', 'JPN225',
]

TIMEFRAMES = ['Auto', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']


@app.route('/')
def dashboard():
    """Serve the main chart dashboard."""
    from flask import make_response
    resp = make_response(render_template('dashboard.html', symbols=SUGGESTED_SYMBOLS, timeframes=TIMEFRAMES))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/sw.js')
def service_worker():
    """Serve PWA service worker from root scope."""
    return send_from_directory('static', 'sw.js',
                               mimetype='application/javascript')

@app.route('/manifest.json')
def manifest():
    """Serve PWA manifest."""
    return send_from_directory('static', 'manifest.json',
                               mimetype='application/manifest+json')


@app.route('/api/analyze')
def api_analyze():
    """
    Run a full chart analysis for the given symbol and timeframe.

    Query params:
        symbol    — MT5 symbol name (default: XAUUSD)
        timeframe — M5 | M15 | M30 | H1 | H4 | D1 | W1 | auto (default: auto)
        refresh   — 'true' to force a fresh analysis, bypassing the cache

    Returns JSON with: candles, pattern, sr_levels, zones, signal, trade, macd,
                       _cached (bool), _age_mins (float)
    """
    symbol    = request.args.get('symbol', GOLD_SYMBOL)
    timeframe = request.args.get('timeframe', 'auto').replace('Auto', 'auto')
    force     = request.args.get('refresh', 'false').lower() == 'true'

    cache_key = f"{symbol}_{timeframe}"

    # ── Serve cached signal if still valid ────────────────────────────────────
    if not force:
        cached = _signal_cache.get(cache_key)
        if cached:
            valid, reason = _cache_valid(cached, symbol)
            if valid:
                data              = dict(cached['data'])
                data['_cached']   = True
                data['_age_mins'] = round((time.time() - cached['ts']) / 60, 1)
                # _in_feed = True only when this symbol+TF is actually locked in the
                # signals feed. _cached=True just means the analysis was cached (happens
                # for every analysis). The dashboard badge uses _in_feed to say
                # "Signal locked" vs "Analysis cached Xm ago".
                data['_in_feed']  = cache_key in _signals_feed
                log.info(f"Cache hit {symbol} {timeframe} — {data['_age_mins']}m old, in_feed={data['_in_feed']}")
                return jsonify(data)
            log.info(f"Cache cleared {symbol} {timeframe}: {reason}")
            _signal_cache.pop(cache_key, None)

    # ── Fresh analysis ────────────────────────────────────────────────────────
    log.info(f"Fresh analysis {symbol} {timeframe}...")
    try:
        result                   = analyze(symbol, timeframe)
        result['_cached']        = False
        result['_age_mins']      = 0
        result['_in_feed']       = cache_key in _signals_feed
        _signal_cache[cache_key] = {'data': result, 'ts': time.time(), 'tf': timeframe}
        log.info(f"Cached — {result['pattern']['name']}, {result['signal']}")
        return jsonify(result)
    except Exception as e:
        log.exception("Analysis failed")
        return jsonify({'error': str(e)}), 500


# ── Locked Signals Feed ──────────────────────────────────────────────────────
# The Signals Feed is different from the scanner:
#   • Only STRONG signals with a REAL chart pattern make it here
#   • Entry / SL / TP are LOCKED the moment the signal fires — never change again
#   • Signal stays active until TP hit, SL crossed, neckline broken, or TTL expires
#   • This is the "final say" — if it's here, take the trade
_signals_feed: dict = {}   # { key: {symbol, tf, data, ts, locked_trade} }
_FEED_FILE = os.path.join(os.path.dirname(__file__), 'signals_feed.json')


def _save_feed():
    """Persist locked feed to disk so signals survive app restarts."""
    try:
        with open(_FEED_FILE, 'w') as f:
            json.dump(_signals_feed, f, default=str)
    except Exception as e:
        log.warning(f"Could not save feed: {e}")


def _load_feed():
    """Restore locked feed from disk on startup — drop any expired entries."""
    global _signals_feed
    if not os.path.exists(_FEED_FILE):
        return
    try:
        with open(_FEED_FILE, 'r') as f:
            raw = json.load(f)
        now = time.time()
        restored = 0
        for key, locked in raw.items():
            tf  = locked.get('tf', 'H1')
            age = now - locked.get('ts', 0)
            if age < _FEED_TTL.get(tf, 3600):
                _signals_feed[key] = locked
                restored += 1
        log.info(f"Restored {restored} locked signals from disk ({len(raw)-restored} expired)")
    except Exception as e:
        log.warning(f"Could not load feed: {e}")

_FEED_TTL = {
    # Signals are now closed ONLY by TP hit, SL hit, or HTF bias flip.
    # TTL is a safety backstop only — if MT5 disconnects and misses
    # a TP/SL event, these ensure stale signals eventually clear.
    'M5':   4 * 60 * 60,        # 4 hours (was 45 min)
    'M15':  12 * 60 * 60,       # 12 hours (was 3h)
    'H1':   7 * 24 * 60 * 60,   # 7 days (was 8h)
    'H4':  30 * 24 * 60 * 60,   # 30 days (was 24h)
    'D1':  90 * 24 * 60 * 60,   # 90 days (was 72h)
}

# ── Gold Scalper Feed ────────────────────────────────────────────────────────
# Separate locked feed for XAUUSD micro-scalp signals.
# Same lock/history logic as main feed — just a different dict and TTL.
_gold_feed: dict        = {}   # { "M5": {tf, data, ts, locked_trade, ...} }
_gold_history: list     = []
_GOLD_HISTORY_FILE      = os.path.join(os.path.dirname(__file__), 'gold_history.json')
_GOLD_FEED_FILE         = os.path.join(os.path.dirname(__file__), 'gold_feed.json')
_gold_cooldowns: dict   = {}   # { "M5": {'expiry': timestamp, 'direction': 'bearish'} }
                                   # Direction-specific: after a SELL loss, only SELL is blocked;
                                   # BUY signals on the same TF can fire immediately.
_gold_watch: dict       = {}   # { "M1": {tf, entry, sl, tp, ...} } — persists across scans

_GOLD_TTL = {
    # Long backstop — FIRE signals close via TP/SL only; TTL is last-resort
    # in case MT5 disconnects and misses a close event.
    'M1': 3600, 'M3': 5400, 'M5': 7200,
}

_GOLD_WATCH_TTL = {
    # How long a WATCH card stays visible after first detected.
    # Extended M1 to 30 min — on fast markets the retrace often takes
    # longer than 10 min, so the old 600s was wiping the card just as
    # price was arriving, leaving the user's limit order orphaned.
    'M1': 1800, 'M3': 3600, 'M5': 7200,
}

_GOLD_COOLDOWN = {
    # Short lockout after any close (WIN or LOSS) — just prevents the same candle
    # from immediately re-locking before the next clean setup forms.
    # The real quality filters are: sweep requirement, M5 alignment, dead OB check.
    # A long cooldown here does more harm than good — it blocks valid signals.
    'M1': 30,   'M3': 90,   'M5': 120,   # M1=30s: faster re-scan after TP/SL
}


# ── Level Gravity feed ────────────────────────────────────────────────────────
# Separate locked-signal store for the gravity scalper.
# Same lock-in pattern as gold feed: FIRE locks the entry until TP or SL is hit.

_gravity_feed:        dict = {}   # { "M1": { locked signal } }
_gravity_cooldowns:   dict = {}   # { "M1": expiry_timestamp }
_gravity_history:     list = []   # list of closed gravity trades (persisted to disk)
_gravity_last_close:  dict = {}   # { "M1": {'direction': 'BUY', 'ts': float} } — last closed trade (WIN or LOSS)
_GRAVITY_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'gravity_history.json')

_GRAVITY_COOLDOWN        = 10  # seconds cooldown after a LOSS
_GRAVITY_WIN_COOLDOWN    = 20  # longer cooldown after a WIN — kills fake pullback signals naturally
_REENTRY_WINDOW          = 30  # seconds after a LOSS where same direction needs 3/3 (don't chase)


def _save_gravity_history():
    try:
        with open(_GRAVITY_HISTORY_FILE, 'w') as f:
            json.dump(_gravity_history[-500:], f, indent=2)
    except Exception as e:
        log.warning(f"Could not save gravity history: {e}")


def _load_gravity_history():
    global _gravity_history
    if os.path.exists(_GRAVITY_HISTORY_FILE):
        try:
            with open(_GRAVITY_HISTORY_FILE, 'r') as f:
                _gravity_history = json.load(f)
            log.info(f"Gravity history loaded: {len(_gravity_history)} trades")
        except Exception as e:
            log.warning(f"Could not load gravity history: {e}")


def _record_gravity_outcome(locked: dict, outcome: str, exit_price: float):
    tp_dist   = abs(locked.get('tp', 0) - locked.get('entry', 0))
    sl_dist   = abs(locked.get('sl', 0) - locked.get('entry', 0))
    entry     = locked.get('entry', 0)
    direction = locked.get('direction', 'BUY')
    if outcome == 'WIN':
        pnl = tp_dist
    else:
        pnl = -sl_dist
    _gravity_history.append({
        'tf':         locked.get('tf', 'M1'),
        'direction':  locked.get('direction'),
        'entry':      locked.get('entry'),
        'tp':         locked.get('tp'),
        'sl':         locked.get('sl'),
        'exit_price': round(exit_price, 2),
        'outcome':    outcome,
        'pnl_pts':    round(pnl, 2),
        'ts':         time.time(),
        'strategy':   'GRAVITY',
    })
    _save_gravity_history()


# ═══════════════════════════════════════════════════════════════════════════════
#  CANDLE SURFER — tick-level candle riding system
#  Fires when a candle commits $0.65+ from its open, trails at $0.20.
#  Separate history, separate panel, separate bot endpoint.
# ═══════════════════════════════════════════════════════════════════════════════

_surfer_feed:        dict = {}   # active signal (empty when flat)
_surfer_history:     list = []   # completed surfer trades
_surfer_candle_open: dict = {}   # {'open': float, 'time': int, 'triggered': bool}

_SURFER_THRESHOLD   = 0.75   # candle must move $0.75 from open before entry — FBS spread $0.50 requires this
_SURFER_TRAIL       = 0.25   # trailing stop distance in $ (caps loss at $0.25 per trade)
_SURFER_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'candle_surfer_history.json')


def _save_surfer_history():
    try:
        with open(_SURFER_HISTORY_FILE, 'w') as f:
            json.dump(_surfer_history[-500:], f, indent=2)
    except Exception as e:
        log.warning(f'[CandleSurfer] history save failed: {e}')


def _load_surfer_history():
    global _surfer_history
    if os.path.exists(_SURFER_HISTORY_FILE):
        try:
            with open(_SURFER_HISTORY_FILE, 'r') as f:
                _surfer_history = json.load(f)
            log.info(f'[CandleSurfer] history loaded: {len(_surfer_history)} trades')
        except Exception as e:
            log.warning(f'[CandleSurfer] history load failed: {e}')


def _record_surfer_outcome(signal: dict, exit_price: float):
    direction = signal.get('direction', 'BUY')
    entry     = signal.get('entry', 0)
    # Entry is already at ASK (spread paid at open), exit at trail stop BID.
    # raw_pnl already includes the spread cost — do NOT subtract spread again.
    raw_pnl   = round((exit_price - entry) if direction == 'BUY' else (entry - exit_price), 2)
    net_pnl   = raw_pnl   # spread already baked into entry vs exit difference
    outcome   = 'WIN' if net_pnl > 0 else 'LOSS'
    _surfer_history.insert(0, {
        'ts'           : time.time(),
        'direction'    : direction,
        'entry'        : entry,
        'exit'         : round(exit_price, 2),
        'raw_pnl'      : raw_pnl,
        'pnl_pts'      : net_pnl,
        'outcome'      : outcome,
        'candle_open'  : signal.get('candle_open', 0),
        'move_at_entry': signal.get('move_at_entry', 0),
    })
    _save_surfer_history()
    log.info(f'[CandleSurfer] {outcome} {direction} pnl={net_pnl:+.2f}pts  exit={exit_price}')


def _candle_surfer_loop():
    import MetaTrader5 as mt5
    global _surfer_feed, _surfer_candle_open
    log.info('[CandleSurfer] Background tick monitor started — checking every 0.5s')
    while True:
        try:
            tick = mt5.symbol_info_tick(GOLD_SYMBOL)
            if tick is None:
                time.sleep(0.5)
                continue

            bid  = round(tick.bid, 2)
            ask  = round(tick.ask, 2)
            live = round((bid + ask) / 2, 2)
            now  = time.time()

            # ── Current M1 candle open ────────────────────────────────────
            rates = mt5.copy_rates_from_pos(GOLD_SYMBOL, mt5.TIMEFRAME_M1, 0, 1)
            if rates is None or len(rates) < 1:
                time.sleep(0.5)
                continue

            candle_time  = int(rates[0]['time'])
            candle_open  = round(float(rates[0]['open']), 2)

            # New candle → reset tracker (active trade keeps running — trail manages exit)
            if candle_time != _surfer_candle_open.get('time', 0):
                _surfer_candle_open = {
                    'open'     : candle_open,
                    'time'     : candle_time,
                    'triggered': False,
                }

            # ── Manage active trade ───────────────────────────────────────
            if _surfer_feed:
                direction  = _surfer_feed['direction']
                trail_stop = _surfer_feed['trail_stop']

                if direction == 'BUY':
                    new_trail = round(bid - _SURFER_TRAIL, 2)
                    if new_trail > trail_stop:
                        _surfer_feed['trail_stop'] = new_trail
                    if bid <= _surfer_feed['trail_stop']:
                        # Exit recorded at trail stop price — not current bid (avoids lag distortion)
                        _record_surfer_outcome(_surfer_feed.copy(), _surfer_feed['trail_stop'])
                        _surfer_feed.clear()
                        # triggered stays True — ONE trade per candle maximum

                elif direction == 'SELL':
                    new_trail = round(ask + _SURFER_TRAIL, 2)
                    if new_trail < trail_stop:
                        _surfer_feed['trail_stop'] = new_trail
                    if ask >= _surfer_feed['trail_stop']:
                        # Exit recorded at trail stop price — not current ask (avoids lag distortion)
                        _record_surfer_outcome(_surfer_feed.copy(), _surfer_feed['trail_stop'])
                        _surfer_feed.clear()
                        # triggered stays True — ONE trade per candle maximum

            # ── Look for new signal ───────────────────────────────────────
            elif not _surfer_candle_open.get('triggered', False):
                c_open = _surfer_candle_open.get('open', live)
                move   = round(live - c_open, 2)

                if abs(move) >= _SURFER_THRESHOLD:
                    if move > 0:
                        direction  = 'BUY'
                        entry      = ask
                        trail_stop = round(bid - _SURFER_TRAIL, 2)   # use BID — exit check is against BID
                    else:
                        direction  = 'SELL'
                        entry      = bid
                        trail_stop = round(ask + _SURFER_TRAIL, 2)   # use ASK — exit check is against ASK

                    _surfer_feed.update({
                        'direction'    : direction,
                        'entry'        : entry,
                        'trail_stop'   : trail_stop,
                        'entry_ts'     : now,
                        'candle_open'  : c_open,
                        'move_at_entry': move,
                        'signal_id'    : str(round(now, 3)),
                    })
                    _surfer_candle_open['triggered'] = True
                    log.info(f'[CandleSurfer] FIRE {direction} entry={entry} move={move:+.2f} trail={trail_stop}')

        except Exception as e:
            log.warning(f'[CandleSurfer] loop error: {e}')

        time.sleep(0.5)


def _start_candle_surfer():
    import threading
    t = threading.Thread(target=_candle_surfer_loop, daemon=True, name='CandleSurfer')
    t.start()
    log.info('[CandleSurfer] Thread started')


def _save_gold_feed():
    try:
        with open(_GOLD_FEED_FILE, 'w') as f:
            json.dump(_gold_feed, f, default=str)
    except Exception as e:
        log.warning(f"Gold feed save failed: {e}")


def _save_gold_history():
    try:
        with open(_GOLD_HISTORY_FILE, 'w') as f:
            json.dump(_gold_history[:500], f, indent=2)
    except Exception as e:
        log.warning(f"Gold history save failed: {e}")


def _load_gold_data():
    global _gold_feed, _gold_history
    now = time.time()
    # Feed
    if os.path.exists(_GOLD_FEED_FILE):
        try:
            with open(_GOLD_FEED_FILE, 'r') as f:
                raw = json.load(f)
            restored = 0
            for tf, locked in raw.items():
                age = now - locked.get('ts', 0)
                ttl = _GOLD_TTL.get(tf, 3600)
                if age < ttl:
                    _gold_feed[tf] = locked
                    restored += 1
                else:
                    # Signal expired while server was down — record it
                    _record_gold_outcome(locked, 'EXPIRED', locked.get('locked_trade', {}).get('entry', 0))
                    log.info(f"[Gold {tf}] expired during restart — recorded as EXPIRED")
            if restored:
                log.info(f"Gold feed restored: {restored} active signal(s)")
        except Exception as e:
            log.warning(f"Gold feed load failed: {e}")
    # History
    if os.path.exists(_GOLD_HISTORY_FILE):
        try:
            with open(_GOLD_HISTORY_FILE, 'r') as f:
                _gold_history = json.load(f)
            log.info(f"Gold history restored: {len(_gold_history)} trades from disk")
        except Exception as e:
            log.warning(f"Gold history load failed: {e}")
    else:
        log.info("Gold history: no file yet (fresh start)")


def _record_gold_outcome(locked: dict, outcome: str, close_price: float):
    t         = locked.get('locked_trade', {})
    entry     = t.get('entry') or 0
    sl        = t.get('sl')   or 0
    tp        = t.get('tp')   or 0
    pnl       = abs(tp - entry) if outcome == 'WIN' else -abs(entry - sl)
    record    = {
        'tf':          locked['tf'],
        'direction':   locked.get('direction', ''),
        'entry':       entry, 'sl': sl, 'tp': tp,
        'rr':          t.get('rr'),
        'outcome':     outcome,
        'close_price': round(close_price, 2),
        'pnl_pts':     round(pnl, 2),
        'ts_fired':    locked['ts'],
        'ts_closed':   time.time(),
        'duration_m':  round((time.time() - locked['ts']) / 60, 1),
    }
    _gold_history.insert(0, record)
    _save_gold_history()
    log.info(f"[Gold] {locked['tf']} {outcome} pnl={pnl:+.2f}")


# ── News Sniper Feed ─────────────────────────────────────────────────────────
# Signals generated by the post-news sniper. Separate from main and gold feeds.
_news_feed: dict       = {}   # { "EURUSD_M5": { ...locked FIRE signal... } }
_news_watch: dict      = {}   # { "EURUSD_M1": { ...WATCH — waiting for price to reach OB... } }
_news_history: list    = []
_NEWS_HISTORY_FILE     = os.path.join(os.path.dirname(__file__), 'news_history.json')
_NEWS_FEED_FILE        = os.path.join(os.path.dirname(__file__), 'news_feed.json')

_NEWS_TTL = {    # seconds a news FIRE signal stays live
    'M1': 300, 'M3': 480, 'M5': 720,
}
_NEWS_WATCH_TTL = {   # news WATCH signals expire faster — OBs go stale after the news move settles
    'M1': 180, 'M3': 300, 'M5': 420,
}


def _save_news_feed():
    try:
        with open(_NEWS_FEED_FILE, 'w') as f:
            json.dump(_news_feed, f, default=str)
    except Exception as e:
        log.warning(f"News feed save failed: {e}")


def _save_news_history():
    try:
        with open(_NEWS_HISTORY_FILE, 'w') as f:
            json.dump(_news_history[:500], f, indent=2)
    except Exception as e:
        log.warning(f"News history save failed: {e}")


def _load_news_data():
    global _news_feed, _news_history
    now = time.time()
    if os.path.exists(_NEWS_FEED_FILE):
        try:
            with open(_NEWS_FEED_FILE, 'r') as f:
                raw = json.load(f)
            for key, locked in raw.items():
                tf  = locked.get('tf', 'M5')
                age = now - locked.get('ts', 0)
                if age < _NEWS_TTL.get(tf, 720):
                    _news_feed[key] = locked
        except Exception as e:
            log.warning(f"News feed load failed: {e}")
    if os.path.exists(_NEWS_HISTORY_FILE):
        try:
            with open(_NEWS_HISTORY_FILE, 'r') as f:
                _news_history = json.load(f)
        except Exception as e:
            log.warning(f"News history load failed: {e}")


def _record_news_outcome(locked: dict, outcome: str, close_price: float):
    entry  = locked.get('entry') or 0
    sl     = locked.get('sl')    or 0
    tp     = locked.get('tp')    or 0
    pnl    = abs(tp - entry) if outcome == 'WIN' else -abs(entry - sl)
    record = {
        'symbol':      locked['symbol'],
        'tf':          locked['tf'],
        'direction':   locked.get('direction', ''),
        'news_title':  locked.get('news_title', ''),
        'news_country': locked.get('news_country', ''),
        'entry':       entry, 'sl': sl, 'tp': tp,
        'rr':          locked.get('rr'),
        'outcome':     outcome,
        'close_price': round(close_price, 4),
        'pnl_pts':     round(pnl, 4),
        'ts_fired':    locked['ts'],
        'ts_closed':   time.time(),
        'duration_m':  round((time.time() - locked['ts']) / 60, 1),
    }
    _news_history.insert(0, record)
    _save_news_history()
    log.info(f"[News] {locked['symbol']} {locked['tf']} {outcome} pnl={pnl:+.4f}")


# ── Post-loss cooldown ────────────────────────────────────────────────────────
# After a LOSS or neckline break, the same symbol+TF is blocked from re-locking
# for a cooldown period. This prevents the "same trade fires 3× in a row" scenario
# seen when EMA Bounce conditions persist even after the setup has already failed.
_feed_cooldowns: dict = {}   # { "SYMBOL_TF": expiry_timestamp }

# ── Watch Alerts ──────────────────────────────────────────────────────────────
# Early-warning alerts: sweep detected + at least one reversal candle, fired
# BEFORE full BOS/FVG/score confirmation. Gives the user time to prepare a
# limit order at the institutional zone WHILE the signal is still fresh.
_watch_alerts: dict  = {}    # { "SYMBOL_TF": alert_dict }
_watch_history: list = []    # closed watch alerts (GRADUATED / EXPIRED / TP / SL)
_WATCH_TTL_BY_TF     = {'M5': 600, 'M15': 1800, 'M30': 3600, 'H1': 14400, 'H4': 43200}
_WATCH_TTL           = 900   # fallback default
_WATCH_COOLDOWN      = 300   # 5 minutes — don't re-fire same symbol+TF
_WATCH_MIN_SCORE     = 3     # only fire Watch if setup scores at least 3/8
_WATCH_HIST_MAX      = 50    # keep last 50 watch outcomes

_COOLDOWN_AFTER_LOSS = {
    'M5':   5 * 60,    #  5 min — M5 moves fast, short cooldown
    'M15': 10 * 60,    # 10 min — backstop only; ADX gate is the real defence
    'H1':  30 * 60,    # 30 min
    'H4':   2 * 3600,
    'D1':  12 * 3600,
}


# ── Trade History ────────────────────────────────────────────────────────────
# Every time a locked signal closes (TP hit, SL crossed, neckline broken, or
# TTL expired), the outcome is recorded here and saved to disk so history
# survives restarts.
_trade_history: list = []
_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'signals_history.json')


def _load_history():
    """Load trade history from disk on startup."""
    global _trade_history
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, 'r') as f:
                _trade_history = json.load(f)
            log.info(f"Loaded {len(_trade_history)} historical trades from disk")
    except Exception as e:
        log.warning(f"Could not load trade history: {e}")
        _trade_history = []


def _save_history():
    """Persist history to disk — keep last 1000 records."""
    try:
        with open(_HISTORY_FILE, 'w') as f:
            json.dump(_trade_history[:1000], f, indent=2)
    except Exception as e:
        log.warning(f"Could not save trade history: {e}")


def _record_outcome(locked: dict, outcome: str, close_price: float):
    """
    Record a closed signal to trade history.
    outcome: 'WIN' | 'LOSS' | 'EXPIRED'
    """
    d = locked['data']
    t = locked['locked_trade']
    p = d.get('pattern', {})
    c = d.get('confluence', {})

    entry     = t.get('entry') or 0
    sl        = t.get('sl')   or 0
    tp        = t.get('tp')   or 0
    direction = p.get('direction', '')

    # Calculate approximate pips/points gained or lost
    if outcome == 'WIN':
        pnl_pts = abs(tp - entry)
    elif outcome in ('LOSS', 'EXPIRED'):
        pnl_pts = -abs(entry - sl)
    else:
        pnl_pts = 0

    record = {
        'symbol':      locked['symbol'],
        'tf':          locked['tf'],
        'direction':   direction,
        'pattern':     p.get('name', ''),
        'score':       c.get('score', 0),
        'max_score':   c.get('max_score', 8),
        'entry':       entry,
        'sl':          sl,
        'tp':          tp,
        'rr':          t.get('rr'),
        'outcome':     outcome,
        'close_price': round(close_price, 5) if close_price else None,
        'pnl_pts':     round(pnl_pts, 5),
        'ts_fired':    locked['ts'],
        'ts_closed':   time.time(),
        'duration_m':  round((time.time() - locked['ts']) / 60, 1),
    }

    _trade_history.insert(0, record)   # newest first
    _save_history()
    log.info(f"Trade history: {record['symbol']} {record['tf']} {outcome} "
             f"entry={entry} close={close_price:.5f} pnl={pnl_pts:+.5f}")


def _is_premium_signal(data: dict) -> bool:
    """
    Premium = only signals worth showing in the locked feed.

    Requirements:
      • STRONG verdict (≥6/8 from scoring engine)
      • Score ≥ 6/8  — real patterns (Pin Bar, EMA Bounce, Double Top/Bottom, Wedge,
                        Round Level Bounce) qualify at 6. EMA Trend is handled separately.
      • Real chart pattern — EMA Trend only allowed at a perfect 8/8
      • R:R ≥ 1.5   — lets clean M5 scalps through; still rejects poor-quality setups
    """
    confluence = data.get('confluence', {})
    pattern    = data.get('pattern', {})
    if confluence.get('verdict') != 'STRONG':
        return False
    score    = confluence.get('score', 0)
    pat_name = pattern.get('name', '')
    # 6/8 is the minimum for real patterns in the signals feed
    if score < 6:
        return False
    # EMA Trend is the weakest pattern — needs a near-perfect score to lock
    if pat_name == 'EMA Trend' and score < 8:
        return False
    # R:R ≥ 1.5 — 1.5:1 is the minimum acceptable risk-to-reward for any trade
    rr = data.get('trade', {}).get('rr') or 0
    if rr < 1.5:
        return False
    # ── Hard session gate for scalp timeframes ────────────────────────────────
    # M5/M15/M30 are SHORT-TERM scalps. Outside London (07-16 UTC) and New York
    # (13-21 UTC) sessions the market is in Asian session: low volume, choppy,
    # no directional momentum. Even a perfect 8/8 score produces trades that
    # either expire (no movement) or hit SL on a random wick. This is the #1
    # cause of the EXPIRED history entries. Session is a HARD BLOCK for scalps.
    # H1 and above span multiple sessions and are not restricted.
    tf = data.get('timeframe', '')
    if tf in ('M5', 'M15', 'M30') and not is_active_session():
        return False   # Asian session — no scalp locks until London opens
    # ── Pin Bar key-level requirement ─────────────────────────────────────────
    # Confidence 0.76 = pin bar AT a key level (PDH/PDL, round number, swing S/R)
    # Confidence 0.62 = pin bar with no significant level nearby = noise
    # A low-confidence pin bar in a quiet market fires and immediately reverses.
    if pat_name == 'Pin Bar' and pattern.get('confidence', 0) < 0.74:
        return False   # generic pin bar with no institutional level = skip
    # EMA Bounce REQUIRES trend confirmation — ADX factor must PASS.
    # EMA20 is only dynamic support/resistance in a TRENDING market.
    # If ADX fails, the market is ranging and EMA20 is just the middle of the
    # range — price will oscillate through it and immediately hit SL.
    # This is a HARD block, not a score penalty. EMA Bounce + ADX fail = never lock.
    factors = confluence.get('factors', {})
    if pat_name == 'EMA Bounce' and not factors.get('ADX', {}).get('pass', False):
        return False
    return True


# ── Replaced by SMC gate below — kept as dead code placeholder ────────────────
# Old indicator-based checks removed in SMC rebuild.
# New SMC gate is enforced in _is_premium_signal_smc (aliased as _is_premium_signal).


def _is_premium_signal_smc(data: dict) -> bool:
    """
    SMC Premium Gate — signals must prove institutional activity before locking.

    NON-NEGOTIABLE CORE MODEL (all three required for STRONG lock):
      1. Liquidity Sweep  — smart money just swept a pool (BSL or SSL)
      2. Displacement     — institutional candle (>=1.5xATR body) after the sweep
      3. R:R >= 2.0:1     — entry at OB/FVG with SL at swept level must produce >=2R

    SUPPORTED SETUPS (one required alongside core model):
      • Unmitigated Order Block near price  (OB entry)
      • FVG imbalance zone near price       (FVG entry)
      • CHoCH confirming reversal           (structure confirmation)
      • BOS confirming continuation         (structure confirmation)

    HARD BLOCKS (always skip regardless of score):
      • M5/M15/M30 outside London (07-10 UTC) or New York (13-16 UTC) kill zones
      • No sweep found (retail pattern — not SMC)
      • No displacement (no institutional follow-through)
    """
    confluence = data.get('confluence', {})
    smc        = data.get('smc', {})
    trade      = data.get('trade', {})
    tf         = data.get('timeframe', '')

    # SMC verdict must be STRONG
    if confluence.get('verdict') != 'STRONG':
        return False

    # Non-negotiable: sweep must exist
    sweep = smc.get('sweep', {})
    if not sweep or not sweep.get('found'):
        return False

    # Non-negotiable: displacement must exist
    disp = smc.get('displacement', {})
    if not disp or not disp.get('found'):
        return False

    # Non-negotiable: R:R >= 2.0
    rr = trade.get('rr') or 0
    if rr < 2.0:
        return False

    # D1 HTF alignment — signal must trade WITH the daily institutional bias.
    # If D1 is bullish, only BUYS are allowed on M5/M15/M30/H1.
    # If D1 is bearish, only SELLS are allowed on M5/M15/M30/H1.
    # This was the root cause of USDCAD M15 SELL locking while D1 was bullish.
    d1_bias   = data.get('d1_bias', 'neutral')
    direction = data.get('pattern', {}).get('direction', '')
    if tf not in ('H4', 'D1') and d1_bias != 'neutral' and d1_bias != direction:
        return False   # trading against D1 — institutional bias opposes this trade

    # Hard session gate for scalp timeframes
    if tf in ('M5', 'M15', 'M30') and not is_active_session():
        return False

    # ── News pause gate ───────────────────────────────────────────────────────
    # During ±2 min of a HIGH-impact news release, the main scanner pauses.
    # Fake spikes during news would stop out valid trades. The news sniper
    # handles entries after the spike settles. This gate only blocks NEW locks.
    try:
        from news_calendar import is_news_active
        if is_news_active(tier='high'):
            return False   # HIGH-impact news window active — no new SMC locks right now
    except Exception:
        pass   # calendar unavailable — don't block trading

    # Structure must pass OR score must be ≥ 6/7
    # If structure is FAILING (trading against confirmed trend), we need near-perfect
    # score across all other factors to compensate. A 5/7 signal with structure failing
    # means 2 factors miss AND the trend is against us — that's the #1 cause of losses.
    factors      = confluence.get('factors', {})
    struct_pass  = factors.get('Structure', {}).get('pass', False)
    score        = confluence.get('score', 0)
    if not struct_pass and score < 6:
        return False   # trend fighting + weak score = skip

    # Require at least one entry zone factor (OB or FVG)
    # BOS/CHoCH alone are structure markers, not entry zones.
    # Without an OB or FVG, the entry has no institutional price origin.
    ob_pass  = factors.get('Order Block', {}).get('pass', False)
    fvg_pass = factors.get('FVG',         {}).get('pass', False)
    bos      = smc.get('bos')
    choch    = smc.get('choch')
    if not (ob_pass or fvg_pass or bool(bos) or bool(choch)):
        return False

    return True


# Alias so all existing callers work without change
_is_premium_signal = _is_premium_signal_smc


def _htf_confirms(scanner_results: dict, symbol: str, tf: str, direction: str) -> bool:
    """
    Before locking a signal, verify the HIGHER timeframe actively agrees.
    Uses already-computed scanner results — no extra MT5 calls.

    Rule:
      • Higher TF bullish/neutral  → allow BUY signal
      • Higher TF bearish (any score ≥ 2) → BLOCK BUY signal
      Same logic reversed for SELL.

    Old behaviour blocked only on STRONG/WATCH verdict. That missed the case
    where HTF is showing a clear bearish pattern but with a low score (SKIP
    verdict) — e.g. M15 actively falling but not strong enough to score STRONG.
    The result: M5 BUY locked while M15 is selling = guaranteed loss.

    New behaviour: block whenever HTF direction opposes AND score ≥ 2.
    Score ≥ 2 means at least 2 confluence factors confirm the HTF direction —
    enough to say the HTF has a real bearish/bullish lean, not just noise.
    """
    HIGHER = {'M5': 'M15', 'M15': 'H1', 'M30': 'H1', 'H1': 'H4', 'H4': 'D1'}
    htf = HIGHER.get(tf)
    if not htf:
        return True   # no higher TF mapped → allow

    htf_key    = f"{symbol}_{htf}"
    htf_result = scanner_results.get(htf_key)
    if not htf_result or not htf_result.get('data'):
        return True   # no HTF data yet → allow (don't block on missing info)

    htf_dir   = htf_result['data'].get('pattern', {}).get('direction', '')
    htf_score = htf_result['data'].get('confluence', {}).get('score', 0)

    # Block if HTF direction opposes AND has at least minimal confluence (score ≥ 2)
    # Score 0-1 = too noisy to trust; score ≥ 2 = real directional lean
    if direction == 'bullish' and htf_dir == 'bearish' and htf_score >= 2:
        log.info(f"Signals feed blocked {symbol} {tf} BUY — {htf} bearish score={htf_score}")
        return False
    if direction == 'bearish' and htf_dir == 'bullish' and htf_score >= 2:
        log.info(f"Signals feed blocked {symbol} {tf} SELL — {htf} bullish score={htf_score}")
        return False

    return True


def _move_consumed_pct(data: dict, live_price: float) -> float:
    """
    How much of the entry→TP move has price already covered?

    Example — XAUUSD BUY:
      Entry = 4,080  |  TP = 4,100  |  Live = 4,092
      Move consumed = (4092 - 4080) / (4100 - 4080) = 60%
      → 60% of the profit is already gone before you even entered

    Thresholds (set per timeframe in the gate):
      M5  → block if > 35% consumed  (5-min candle moves fast)
      M15 → block if > 25% consumed
      H1+ → block if > 20% consumed
    """
    t         = data.get('trade', {})
    entry     = t.get('entry')
    tp        = t.get('tp')
    direction = data.get('pattern', {}).get('direction', '')
    if not entry or not tp or not live_price:
        return 0.0
    total = abs(tp - entry)
    if total == 0:
        return 0.0
    consumed = (live_price - entry) if direction == 'bullish' else (entry - live_price)
    return max(0.0, consumed / total * 100)


def _breakout_candle_strong(data: dict, direction: str) -> bool:
    """
    Is the most recent closed candle a STRONG breakout candle?
    A strong candle: body ≥ 40% of its full range (high - low).
    A weak candle (lots of wicks, tiny body) = indecision — not a clean breakout.

    Candle-based patterns (Pin Bar, EMA Bounce, Round Level Bounce) are EXEMPT
    from this gate — their candle quality is already verified during pattern detection.
    Applying the body check to a Pin Bar would always block it (Pin Bars have tiny bodies
    by definition), even though a Pin Bar IS the high-quality signal we want.
    """
    pat_name = data.get('pattern', {}).get('name', '')
    # Candle-based patterns validate their own candle structure — skip this gate
    if pat_name in ('Pin Bar', 'EMA Bounce', 'Round Level Bounce'):
        return True
    try:
        candles = data.get('candles', [])
        if len(candles) < 2:
            return True   # can't check → allow
        # Check the last COMPLETED candle (candles[-2]) — the forming candle (candles[-1])
        # may have just opened with a tiny body even though the move is real.
        last = candles[-2]
        high  = last.get('high', 0)
        low   = last.get('low', 0)
        close = last.get('close', 0)
        open_ = last.get('open', 0)
        rng   = high - low
        if rng == 0:
            return True
        body     = abs(close - open_)
        body_pct = body / rng
        is_bull  = close > open_
        # Candle must be: directionally correct + body ≥ 40% of range
        if direction == 'bullish':
            return is_bull and body_pct >= 0.40
        if direction == 'bearish':
            return (not is_bull) and body_pct >= 0.40
    except Exception:
        pass
    return True   # on error → allow


def _entry_candle_ok(data: dict, direction: str) -> bool:
    """
    Confirm the most recent candle is moving IN the signal direction.
    This prevents entering a BUY right after a big red candle, or a SELL
    right after a big green candle — both are poor timing.

    Check the last TWO closed candles: at least one must confirm.
    """
    try:
        candles = data.get('candles', [])
        if len(candles) < 2:
            return True   # can't check → allow

        def is_bull(c): return c.get('close', 0) > c.get('open', 0)

        last = candles[-1]
        prev = candles[-2]

        if direction == 'bullish':
            return is_bull(last) or is_bull(prev)
        if direction == 'bearish':
            return not is_bull(last) or not is_bull(prev)
    except Exception:
        pass
    return True   # on error, allow


def _feed_signal_valid(locked: dict, symbol: str) -> tuple:
    """Check if a locked signal is still live — same logic as _cache_valid."""
    age = time.time() - locked['ts']
    tf  = locked.get('tf', 'M15')
    if age > _FEED_TTL.get(tf, 3600):
        return False, 'expired'

    trade     = locked['locked_trade']
    direction = locked['data'].get('pattern', {}).get('direction')
    sl        = trade.get('sl')
    tp        = trade.get('tp')
    if not direction or not sl or not tp:
        return True, 'valid'

    price = _current_price(symbol)
    if price is None:
        return True, 'valid'

    if direction == 'bearish':
        if price >= sl: return False, 'sl_crossed'
        if price <= tp: return False, 'tp_hit'
    elif direction == 'bullish':
        if price <= sl: return False, 'sl_crossed'
        if price >= tp: return False, 'tp_hit'

    # NOTE: neckline check deliberately removed.
    # The sniper entry model locks when price RETRACES to the OB/FVG zone,
    # which is by definition BELOW the BOS neckline. Checking neckline here
    # would immediately invalidate every valid locked signal.
    # SL is the only invalidation guard needed once we're in a locked trade.

    return True, 'valid'


def _batch_prices(symbols: list) -> dict:
    """Fetch current MT5 tick prices for a list of symbols in one shot.
    Returns {symbol: mid_price}. Fast — just tick data, no OHLC."""
    prices = {}
    try:
        _mt5_init()
        for sym in symbols:
            tick = mt5.symbol_info_tick(sym)
            if tick:
                prices[sym] = (tick.bid + tick.ask) / 2
    except Exception:
        pass
    return prices


@app.route('/api/scanner-results')
def scanner_results():
    """
    Returns the latest cached scan results from the background scanner.
    Before returning, validates each signal against current live price:
      • BUY  — entry must be ≤ current price (not above market)
      • SELL — entry must be ≥ current price (not below market)
    Stale signals (price has moved past entry in wrong direction) are
    filtered out so the user never sees an unactionable entry.
    """
    results, current, progress, last_ts = scanner.get_results()

    # Batch-fetch current prices for all symbols in one pass
    all_symbols = list({r['symbol'] for r in results.values() if r['data']})
    live_prices = _batch_prices(all_symbols)

    rows = []
    for key, r in results.items():
        if not r['data']:
            continue
        d = r['data']
        c = d.get('confluence', {})
        t = d.get('trade', {})
        direction = d.get('pattern', {}).get('direction', '')
        entry     = t.get('entry')
        sl        = t.get('sl')
        tp        = t.get('tp')
        symbol    = r['symbol']
        live      = live_prices.get(symbol)

        # ── Staleness check ───────────────────────────────────────────────
        # Drop signals where price has already moved past entry in the wrong
        # direction — these are unactionable and confuse the user.
        if live and entry:
            if direction == 'bullish' and live < entry * 0.999:
                # Price dropped below entry (would need to rise to entry) — stale buy
                continue
            if direction == 'bearish' and live > entry * 1.001:
                # Price rose above entry (would need to fall to entry) — stale sell
                continue
            # Also drop if SL has already been hit
            if sl and direction == 'bullish' and live <= sl:
                continue
            if sl and direction == 'bearish' and live >= sl:
                continue

        rows.append({
            'symbol':    symbol,
            'tf':        r['tf'],
            'direction': direction,
            'pattern':   d.get('pattern', {}).get('name', '—'),
            'verdict':   c.get('verdict', 'SKIP'),
            'score':     c.get('score', 0),
            'max_score': c.get('max_score', 7),
            'signal':    d.get('signal', ''),
            'entry':     entry,
            'sl':        sl,
            'tp':        tp,
            'rr':        t.get('rr'),
            'price':     live or d.get('price'),   # show live price, fallback to scan price
            'age_mins':  round((time.time() - r['ts']) / 60, 1),
        })

    order = {'STRONG': 0, 'WATCH': 1, 'SKIP': 2}
    rows.sort(key=lambda x: (order.get(x['verdict'], 3), -(x['score'] or 0)))

    return jsonify({
        'results':      rows,
        'current':      current,
        'progress':     progress,
        'last_scan_ts': last_ts,
        'scan_symbols': scanner.SCAN_SYMBOLS,
        'scan_tfs':     scanner.SCAN_TIMEFRAMES,
    })


def _watch_close(key: str, outcome: str, close_price: float = None) -> None:
    """Move a watch alert to history with a given outcome."""
    global _watch_history
    a = _watch_alerts.pop(key, None)
    if not a:
        return
    record = dict(a)
    record['outcome']     = outcome       # GRADUATED / EXPIRED / TP / SL
    record['close_price'] = close_price
    record['closed_ts']   = time.time()
    _watch_history.insert(0, record)
    if len(_watch_history) > _WATCH_HIST_MAX:
        _watch_history.pop()
    log.info(f"[Watch] {key} closed → {outcome}")


def _update_watch_alerts(results: dict) -> None:
    """
    Scan all scanner results for early sweep+displacement setups AND
    for volatility spikes (news-like candles ≥ 3× ATR), firing WATCH alerts
    before full BOS/FVG/score confirmation.

    Also monitors open watch alerts for TP/SL hits and TTL expiry, and moves
    closed alerts into watch history.
    """
    now = time.time()

    # ── Monitor existing alerts: TP/SL hit or TTL expired ────────────────────
    _OUTCOME_GRACE = 90   # seconds to keep a TP/SL-hit card visible before moving to history

    for key in list(_watch_alerts.keys()):
        a = _watch_alerts.get(key)
        if not a:
            continue

        # If already has an outcome, wait for grace period then close to history
        if a.get('outcome'):
            if now - a.get('outcome_ts', now) >= _OUTCOME_GRACE:
                _watch_close(key, a['outcome'], a.get('outcome_price'))
            continue  # don't re-evaluate while showing outcome

        # TTL expiry per timeframe (M5=10m, M15=30m, M30=1h, H1=4h, H4=12h)
        ttl = _WATCH_TTL_BY_TF.get(a.get('tf', ''), _WATCH_TTL)
        if now - a['ts'] > ttl:
            _watch_close(key, 'EXPIRED')
            continue

        # Update live price
        lp = _current_price(a['symbol'])
        if lp:
            a['live'] = lp

        # ── Vol-spike-only alerts: NO TP/SL auto-close ──────────────────────
        # Their zone is the spike candle itself, so TP would be hit instantly.
        # They are informational only — they expire via TTL.
        if a.get('vol_spike_only'):
            continue

        # Price monitoring vs TP/SL for sweep+displacement alerts
        tp = a.get('tp')
        sl = a.get('sl')
        if lp and tp and sl:
            hit = False
            if a['direction'] == 'bullish':
                if lp >= tp:
                    a['outcome'] = 'TP'; a['outcome_ts'] = now; a['outcome_price'] = lp; hit = True
                elif lp <= sl:
                    a['outcome'] = 'SL'; a['outcome_ts'] = now; a['outcome_price'] = lp; hit = True
            else:
                if lp <= tp:
                    a['outcome'] = 'TP'; a['outcome_ts'] = now; a['outcome_price'] = lp; hit = True
                elif lp >= sl:
                    a['outcome'] = 'SL'; a['outcome_ts'] = now; a['outcome_price'] = lp; hit = True
            if hit:
                log.info(f"[Watch] {a['symbol']} {a['tf']} {a['outcome']} hit @ {lp} — showing 90s then closing")

    # ── Fire new alerts ───────────────────────────────────────────────────────
    for key, r in results.items():
        if not r.get('data'):
            continue
        data   = r['data']
        symbol = r['symbol']
        tf     = r['tf']

        # Skip if full confirmed signal already locked
        if key in _signals_feed:
            continue

        sweep        = data.get('sweep', {})
        displacement = data.get('displacement', {})
        vol_spike    = data.get('vol_spike', {})
        pattern      = data.get('pattern', {})
        direction    = pattern.get('direction', '')

        # Fire condition: sweep+displacement OR a volatility spike
        has_sweep_disp = sweep.get('found') and displacement.get('found') and direction
        has_vol_spike  = vol_spike.get('found') and vol_spike.get('direction')

        if not (has_sweep_disp or has_vol_spike):
            continue

        # Use vol_spike direction if no sweep direction
        if not direction and has_vol_spike:
            direction = vol_spike['direction']
        if not direction:
            continue

        # ── D1 bias filter: only allow alerts aligned with daily trend ────────
        # This ensures one clear direction per symbol — no BUY+SELL confusion.
        d1_bias = data.get('d1_bias', 'neutral')
        if d1_bias != 'neutral':
            if d1_bias == 'bullish' and direction == 'bearish':
                log.debug(f"[Watch] {symbol} {tf} SELL skipped — D1 bias is bullish")
                continue
            if d1_bias == 'bearish' and direction == 'bullish':
                log.debug(f"[Watch] {symbol} {tf} BUY skipped — D1 bias is bearish")
                continue

        # Minimum score gate — only fire Watch if setup already has some structure.
        # This greatly improves graduation rate (weak 1-2/8 setups almost never signal).
        score = data.get('confluence', {}).get('score', 0)
        if not has_vol_spike and score < _WATCH_MIN_SCORE:
            log.debug(f"[Watch] {symbol} {tf} skipped — score {score} < min {_WATCH_MIN_SCORE}")
            continue

        # Determine trigger reason
        if has_vol_spike and not has_sweep_disp:
            reason = f"Volatility spike {vol_spike.get('atr_mult',0)}× ATR — possible news move"
        elif has_vol_spike:
            reason = f"Sweep + Displacement + {vol_spike.get('atr_mult',0)}× ATR spike"
        else:
            reason = 'Sweep + Displacement'

        # One alert per symbol+TF — if one already exists, skip cooldown
        existing = _watch_alerts.get(key)
        if existing and (now - existing['ts']) < _WATCH_COOLDOWN:
            continue

        watch_key = key  # single key per symbol+TF, direction enforced by D1 filter

        # Build zone
        entry_zone  = data.get('entry_zone') or {}
        atr_val     = data.get('atr_current', 0.001)
        swept_level = sweep.get('swept_level')
        live_now    = _current_price(symbol)
        score       = data.get('confluence', {}).get('score', 0)

        if entry_zone and entry_zone.get('low') and entry_zone.get('high'):
            zone_low  = entry_zone['low']
            zone_high = entry_zone['high']
        elif swept_level:
            if direction == 'bullish':
                zone_low  = round(swept_level - atr_val * 0.3, 5)
                zone_high = round(swept_level + atr_val * 0.5, 5)
            else:
                zone_low  = round(swept_level - atr_val * 0.5, 5)
                zone_high = round(swept_level + atr_val * 0.3, 5)
        elif has_vol_spike:
            # For pure vol-spike alerts use the spike candle as zone
            zone_low  = round(vol_spike['candle_low'],  5)
            zone_high = round(vol_spike['candle_high'], 5)
        else:
            continue

        # Compute basic SL/TP for the watch alert
        zone_mid = round((zone_low + zone_high) / 2, 5)
        if direction == 'bullish':
            entry_price = zone_low
            sl_price    = round(zone_low - atr_val * 1.0, 5)
            risk        = entry_price - sl_price
            tp_price    = round(entry_price + risk * 1.5, 5)
        else:
            entry_price = zone_high
            sl_price    = round(zone_high + atr_val * 1.0, 5)
            risk        = sl_price - entry_price
            tp_price    = round(entry_price - risk * 1.5, 5)

        _watch_alerts[key] = {
            'symbol':        symbol,
            'tf':            tf,
            'direction':     direction,
            'swept_level':   swept_level,
            'zone_low':      zone_low,
            'zone_high':     zone_high,
            'entry':         entry_price,
            'sl':            sl_price,
            'tp':            tp_price,
            'rr':            2.0,
            'live':          live_now,
            'score':         score,
            'max_score':     data.get('confluence', {}).get('max_score', 8),
            'pattern':       pattern.get('name', 'Watch'),
            'reason':        reason,
            # vol_spike_only = True means no TP/SL auto-close (zone is spike candle itself)
            'vol_spike_only': has_vol_spike and not has_sweep_disp,
            'ts':            now,
            'seen':          False,
        }
        log.info(f"[Watch] Alert: {symbol} {tf} {direction} — {reason} zone {zone_low}–{zone_high}")


@app.route('/api/signals-feed')
def signals_feed():
    """
    Locked high-confidence signal feed — the 'final say' panel.

    How it works:
      1. Reads the latest scanner results (already computed in background).
      2. Filters for STRONG signals with real chart patterns.
      3. When a qualifying signal appears for the first time, it is LOCKED —
         entry, SL, and TP are frozen at that moment and never change.
      4. The signal stays in the feed until it resolves (TP hit, SL crossed,
         neckline broken) or its TTL expires.
      5. Returns the feed sorted by TF speed (M5 first) then score.
    """
    global _signals_feed
    results, _, _, _ = scanner.get_results()

    # Fire watch alerts FIRST — before signals lock — so setups with sweep+displacement
    # that pass all gates still get a watch alert in the same cycle they lock.
    _update_watch_alerts(results)

    # ── Ingest new qualifying signals from scanner ────────────────────────────
    near_misses   = 0   # strong signals that almost passed but failed one gate
    total_strong  = 0   # total STRONG signals seen in scanner
    for key, r in results.items():
        if not r.get('data'):
            continue
        data   = r['data']
        symbol = r['symbol']
        tf     = r['tf']

        # Count STRONG signals from scanner for near-miss tracking
        sc_verdict = data.get('confluence', {}).get('verdict', '')
        if sc_verdict == 'STRONG':
            total_strong += 1

        # ── Gate 1: Basic premium quality check ──────────────────────────
        if not _is_premium_signal(data):
            continue

        direction = data.get('pattern', {}).get('direction', '')

        # ── Cooldown check — block re-locking after a recent LOSS ────────
        # Prevents the same losing setup (e.g. EMA Bounce in ranging market)
        # from re-locking 2-3× in a row, multiplying a single bad idea into
        # 3 separate losses on the same symbol+TF.
        if _feed_cooldowns.get(key, 0) > time.time():
            log.debug(f"Signals feed cooldown active: {symbol} {tf} — skipping re-lock")
            continue

        near_misses += 1   # passed gate 1 — count as near miss until all gates clear

        # ── Gate 2: Higher timeframe must not actively oppose ─────────────
        if not _htf_confirms(results, symbol, tf, direction):
            continue

        # ── Gate 3: SNIPER — price must be at the OB/FVG entry zone ─────────
        # After a sweep + displacement, price moves AWAY from the entry zone.
        # We do NOT lock mid-displacement. The scanner keeps checking every 15-45s.
        # When price RETRACES back to the OB/FVG zone, this gate passes and we lock.
        # If price never retraces, detect_liquidity_sweep expires (6-candle window)
        # and the setup naturally invalidates. This eliminates all chasing entries.
        live_now   = _current_price(symbol)
        entry_zone = data.get('entry_zone', {})
        atr_val    = data.get('atr_current', 0.001)

        if live_now and entry_zone and entry_zone.get('type') not in (None, 'price'):
            zone_low  = entry_zone.get('low',  0)
            zone_high = entry_zone.get('high', 0)
            # ── Early-entry zone gate ────────────────────────────────────────
            # Buffer is ASYMMETRIC: wide on the approach side (1.5 ATR) to catch
            # near-OB reversals BEFORE price reaches the exact OB, tight on the
            # far side (0.3 ATR) so we don't fire on a blow-through.
            # For a BULLISH setup, price approaches FROM ABOVE (retracing down),
            # so we widen the UPPER buffer. For BEARISH it's the LOWER buffer.
            direction_chk = data.get('pattern', {}).get('direction', '')
            if direction_chk == 'bullish':
                in_zone = (zone_low - atr_val * 0.3) <= live_now <= (zone_high + atr_val * 1.5)
            elif direction_chk == 'bearish':
                in_zone = (zone_low - atr_val * 1.5) <= live_now <= (zone_high + atr_val * 0.3)
            else:
                in_zone = (zone_low - atr_val * 0.5) <= live_now <= (zone_high + atr_val * 0.5)
            if not in_zone:
                log.debug(
                    f"[Sniper] {symbol} {tf} {direction_chk} — "
                    f"live={live_now:.5f} waiting for zone "
                    f"[{zone_low:.5f}–{zone_high:.5f}]"
                )
                continue   # not approaching OB/FVG yet — scanner catches it next cycle
        elif live_now:
            # No institutional zone identified (rare) — use consume gate as fallback
            consumed     = _move_consumed_pct(data, live_now)
            max_consumed = {'M5': 30, 'M15': 20, 'M30': 18, 'H1': 22, 'H4': 25, 'D1': 30}.get(tf, 20)
            if consumed > max_consumed:
                log.info(f"[Sniper fallback] {symbol} {tf}: {consumed:.1f}% consumed — no OB/FVG zone")
                continue

        if key in _signals_feed:
            near_misses -= 1   # already locked — not a near miss
            valid, reason = _feed_signal_valid(_signals_feed[key], symbol)
            if not valid:
                log.info(f"Signals feed removed {symbol} {tf}: {reason}")
                _signals_feed.pop(key, None)
            continue

        near_misses -= 1   # about to lock — not a near miss
        # ── All gates passed — determine entry mode and lock ─────────────
        #
        # The consumed% formula is designed for trend entries, NOT OB-retrace.
        # For OB-retrace we use price POSITION relative to the OB zone instead:
        #
        # BULLISH trade (buy at OB below current price, retrace downward):
        #   • live > zone_low  → approaching from above OR in zone → MARKET at live price
        #   • live < zone_low  → blew through OB (scanner slow)   → LIMIT at OB midpoint
        #
        # BEARISH trade (sell at OB above current price, retrace upward):
        #   • live >= zone_low → in zone OR above it (retrace reached OB) → MARKET at live price
        #   • live < zone_low  → hasn't retraced to OB yet               → LIMIT at OB midpoint
        #
        # In all MARKET cases entry is set to live price so the card shows what
        # you actually pay if you enter at market now.
        # In all LIMIT cases entry stays at OB midpoint — the limit order price.
        # If consumed > 45% (move over) we skip regardless.
        confluence    = data.get('confluence', {})
        locked_trade  = dict(data.get('trade', {}))
        ob_zone_high  = entry_zone.get('high', 0) if entry_zone else 0
        ob_zone_low   = entry_zone.get('low',  0) if entry_zone else 0
        direction_lock = data.get('pattern', {}).get('direction', '')

        if ob_zone_high and ob_zone_low and live_now and direction_lock:
            if direction_lock == 'bullish':
                if live_now >= ob_zone_low:
                    # Approaching from above or in-zone → MARKET
                    entry_mode       = 'market'
                    consumed_at_lock = 0.0
                    locked_trade     = dict(locked_trade)
                    locked_trade['entry'] = round(live_now, 5)
                else:
                    # Below zone (scanner caught it late) → LIMIT at OB
                    entry_mode       = 'limit'
                    consumed_at_lock = max(0.0, _move_consumed_pct(data, live_now))
                    if consumed_at_lock > 45:
                        log.info(f"[Freshness] {symbol} {tf} bullish: {consumed_at_lock:.1f}% consumed — move over, skipping")
                        continue
            else:  # bearish
                if live_now >= ob_zone_low:
                    # In zone or above it (retrace reached OB or overshot) → MARKET
                    entry_mode       = 'market'
                    consumed_at_lock = 0.0
                    locked_trade     = dict(locked_trade)
                    locked_trade['entry'] = round(live_now, 5)
                else:
                    # Below zone (price hasn't retraced to OB yet) → LIMIT at OB
                    entry_mode       = 'limit'
                    consumed_at_lock = max(0.0, _move_consumed_pct(data, live_now))
                    if consumed_at_lock > 45:
                        log.info(f"[Freshness] {symbol} {tf} bearish: {consumed_at_lock:.1f}% consumed — move over, skipping")
                        continue
        else:
            # No OB zone — fallback: use consume% heuristic
            consumed_at_lock = max(0.0, _move_consumed_pct(data, live_now) if live_now else 0)
            max_fresh = {'M5': 35, 'M15': 40, 'M30': 45, 'H1': 45}.get(tf, 40)
            if consumed_at_lock > max_fresh:
                log.info(f"[Freshness] {symbol} {tf}: {consumed_at_lock:.1f}% consumed (no zone) — skipping")
                continue
            entry_mode = 'market' if consumed_at_lock < 15 else 'limit'
        _signals_feed[key] = {
            'symbol':           symbol,
            'tf':               tf,
            'data':             data,
            'ts':               time.time(),
            'locked_trade':     locked_trade,
            'consumed_at_lock': round(consumed_at_lock, 1),
            'entry_mode':       entry_mode,   # 'market' | 'limit'
            # Score/factors frozen at fire time — never degrade in the UI
            'locked_score':     confluence.get('score', 0),
            'locked_factors':   confluence.get('factors', {}),
            'locked_verdict':   confluence.get('verdict', 'STRONG'),
            'locked_max':       confluence.get('max_score', 8),
        }
        # Graduate watch alert → full signal
        _watch_close(key, 'GRADUATED')
        log.info(f"Signals feed LOCKED {symbol} {tf}: "
                 f"{data.get('pattern',{}).get('name')} {direction} "
                 f"score={confluence.get('score')}/8 "
                 f"entry={locked_trade.get('entry')}")
        _save_feed()   # persist immediately so restarts don't lose the lock

    # ── Validate existing locked signals — record outcomes before removing ───
    for key in list(_signals_feed.keys()):
        locked = _signals_feed[key]
        valid, reason = _feed_signal_valid(locked, locked['symbol'])
        if not valid:
            close_price = _current_price(locked['symbol']) or 0
            if reason == 'tp_hit':
                _record_outcome(locked, 'WIN', close_price)
            elif reason == 'sl_crossed':
                _record_outcome(locked, 'LOSS', close_price)
                # Set cooldown — block same symbol+TF from re-locking immediately
                # This prevents 3× same loss when EMA Bounce conditions persist after SL hit
                cd = _COOLDOWN_AFTER_LOSS.get(locked['tf'], 1800)
                _feed_cooldowns[key] = time.time() + cd
                log.info(f"Cooldown set: {locked['symbol']} {locked['tf']} blocked for {cd // 60}m after LOSS")
            elif reason == 'expired':
                _record_outcome(locked, 'EXPIRED', close_price)
            log.info(f"Signals feed removed {locked['symbol']} {locked['tf']}: {reason}")
            _signals_feed.pop(key, None)
            _save_feed()   # persist removal so restart doesn't resurrect a dead signal

    # ── Build response ────────────────────────────────────────────────────────
    tf_order = {'M5': 0, 'M15': 1, 'M30': 2, 'H1': 3, 'H4': 4, 'D1': 5}
    feed     = []
    all_syms = [v['symbol'] for v in _signals_feed.values()]
    prices   = _batch_prices(all_syms)

    for key, locked in _signals_feed.items():
        d      = locked['data']
        t      = locked['locked_trade']   # always the locked-in values
        p      = d.get('pattern', {})
        symbol = locked['symbol']
        tf     = locked['tf']
        live   = prices.get(symbol)
        age_s  = time.time() - locked['ts']
        age_m  = round(age_s / 60, 1)

        # Determine if entry is still reachable
        direction = p.get('direction', '')
        entry     = t.get('entry')
        sl        = t.get('sl')
        tp        = t.get('tp')
        actionable = True
        if live and entry:
            if direction == 'bullish' and live < entry * 0.997:
                actionable = False
            if direction == 'bearish' and live > entry * 1.003:
                actionable = False

        # ── Reversal detection ────────────────────────────────────────────
        # A trade is a REVERSAL when smart money has changed character (CHoCH)
        # — price was trending one way, a liquidity sweep flipped it the other.
        # BOS = continuation (trend still intact). CHoCH = reversal (new direction).
        smc_data    = d.get('smc', {})
        is_reversal = (
            'CHoCH' in p.get('name', '') or
            bool(smc_data.get('choch'))
        )

        feed.append({
            'symbol':     symbol,
            'tf':         tf,
            'direction':  direction,
            'pattern':    p.get('name', ''),
            'confidence': round(p.get('confidence', 0) * 100),
            # ALWAYS use locked score/factors — never the live scanner values
            # This means the card NEVER degrades from 7/8 to 5/8 after entry
            'verdict':    locked.get('locked_verdict', 'STRONG'),
            'score':      locked.get('locked_score', 0),
            'max_score':  locked.get('locked_max', 8),
            'factors':    locked.get('locked_factors', {}),
            'entry':      entry,
            'sl':         sl,
            'tp':         tp,
            'rr':         t.get('rr'),
            'price':      live,
            'actionable':      actionable,
            'age_mins':        age_m,
            'age_secs':        round(age_s),
            'consumed_at_lock': locked.get('consumed_at_lock', 0),
            'entry_mode':      locked.get('entry_mode', 'market'),
            'ts':              locked['ts'],
            'neckline':        p.get('neckline'),
            'is_reversal':     is_reversal,
            'd1_bias':         d.get('d1_bias', 'neutral'),
        })

    feed.sort(key=lambda x: (
        tf_order.get(x['tf'], 9),   # M5 first
        -(x['score'] or 0),          # highest score first
    ))

    return jsonify({
        'signals':      feed,
        'count':        len(feed),
        'near_misses':  max(0, near_misses),   # strong signals being evaluated
        'total_strong': total_strong,           # total STRONG in scanner
        'watch_unseen': sum(1 for a in _watch_alerts.values() if not a['seen']),
    })


@app.route('/api/trade-history')
def trade_history_endpoint():
    """
    Returns the history of all closed signals from the feed.
    Each record shows: symbol, direction, pattern, outcome (WIN/LOSS/EXPIRED),
    entry, sl, tp, close_price, duration, pnl_pts.
    Also returns aggregate stats: total, wins, losses, win_rate.
    """
    decided = [t for t in _trade_history if t['outcome'] in ('WIN', 'LOSS')]
    wins    = sum(1 for t in decided if t['outcome'] == 'WIN')
    losses  = sum(1 for t in decided if t['outcome'] == 'LOSS')
    total_d = wins + losses
    win_rate = round(wins / total_d * 100) if total_d > 0 else 0

    # Best streak
    streak = cur_streak = 0
    last_outcome = None
    for t in reversed(decided):
        if t['outcome'] == last_outcome:
            cur_streak += 1
        else:
            cur_streak  = 1
            last_outcome = t['outcome']
    # Current streak (from start of list = most recent)
    cur_streak = 0
    if decided:
        first = decided[0]['outcome']
        for t in decided:
            if t['outcome'] == first:
                cur_streak += 1
            else:
                break

    stats = {
        'total':       len(_trade_history),
        'decided':     total_d,
        'wins':        wins,
        'losses':      losses,
        'expired':     sum(1 for t in _trade_history if t['outcome'] == 'EXPIRED'),
        'win_rate':    win_rate,
        'streak':      cur_streak,
        'streak_type': decided[0]['outcome'] if decided else None,
    }

    return jsonify({'history': _trade_history[:100], 'stats': stats})


# ── Watch Alerts endpoints ────────────────────────────────────────────────────

@app.route('/api/watch-alerts')
def watch_alerts_endpoint():
    """
    Returns current WATCH alerts — early sweep+reversal detections that
    haven't yet reached full signal confirmation.
    """
    now    = time.time()
    alerts = []
    for key, a in sorted(_watch_alerts.items(), key=lambda x: -x[1]['ts']):
        age_s = now - a['ts']
        alerts.append({
            'key':            key,
            'symbol':         a['symbol'],
            'tf':             a['tf'],
            'direction':      a['direction'],
            'swept_level':    a.get('swept_level'),
            'zone_low':       a['zone_low'],
            'zone_high':      a['zone_high'],
            'entry':          a.get('entry'),
            'sl':             a.get('sl'),
            'tp':             a.get('tp'),
            'rr':             a.get('rr', 2.0),
            'live':           _current_price(a['symbol']),
            'score':          a['score'],
            'max_score':      a.get('max_score', 8),
            'pattern':        a.get('pattern', 'Watch'),
            'reason':         a.get('reason', 'Sweep + Displacement'),
            'age_secs':       round(age_s),
            'seen':           a['seen'],
            'outcome':        a.get('outcome'),          # 'TP'/'SL' if hit, else None
            'vol_spike_only': a.get('vol_spike_only', False),
        })
    unseen = sum(1 for a in _watch_alerts.values() if not a['seen'])
    return jsonify({'alerts': alerts, 'unseen': unseen})


@app.route('/api/watch-seen', methods=['POST'])
def watch_seen_endpoint():
    """Mark all current watch alerts as seen (clears the badge count)."""
    for a in _watch_alerts.values():
        a['seen'] = True
    return jsonify({'ok': True})


@app.route('/api/watch-history')
def watch_history_endpoint():
    """Returns closed watch alerts — GRADUATED / EXPIRED / TP / SL outcomes."""
    hist = []
    for a in _watch_history:
        dur_m = round((a.get('closed_ts', a['ts']) - a['ts']) / 60)
        hist.append({
            'symbol':      a['symbol'],
            'tf':          a['tf'],
            'direction':   a['direction'],
            'outcome':     a['outcome'],
            'zone_low':    a.get('zone_low'),
            'zone_high':   a.get('zone_high'),
            'entry':       a.get('entry'),
            'sl':          a.get('sl'),
            'tp':          a.get('tp'),
            'close_price': a.get('close_price'),
            'reason':      a.get('reason', 'Sweep + Displacement'),
            'pattern':     a.get('pattern', 'Watch'),
            'duration_m':  dur_m,
            'ts':          a['ts'],
        })
    wins    = sum(1 for h in hist if h['outcome'] == 'TP')
    losses  = sum(1 for h in hist if h['outcome'] == 'SL')
    decided = wins + losses
    return jsonify({
        'history': hist,
        'stats': {
            'total':    len(hist),
            'wins':     wins,
            'losses':   losses,
            'decided':  decided,
            'win_rate': round(wins / decided * 100) if decided else 0,
        },
    })


@app.route('/api/gold-feed')
def gold_feed_endpoint():
    """
    Gold Scalper live feed — locks XAUUSD micro-scalp signals when
    price retraces to the micro OB during a kill zone.
    Same lock/expiry/history model as the main signals feed.
    """
    global _gold_feed
    results = scanner.get_gold_results()
    now     = time.time()

    # ── Validate ALL currently locked gold signals (TP / SL / TTL) ───────────
    # This runs BEFORE the ingestion loop and uses a fresh live price directly.
    # The old code put this check inside the `verdict == 'FIRE'` gate, meaning
    # TP/SL were never detected after the OB was consumed (scanner returned SKIP).
    # Result: FIRE cards sat on screen for hours and never moved to history.
    live_gold = _current_price(GOLD_SYMBOL)
    for tf in list(_gold_feed.keys()):
        locked    = _gold_feed[tf]
        t         = locked.get('locked_trade', {})
        direction = locked.get('direction', '')
        sl        = t.get('sl', 0)
        tp        = t.get('tp', 0)
        age       = now - locked.get('ts', 0)
        live      = live_gold

        # Check TP hit
        if live and tp:
            if direction == 'bullish' and live >= tp:
                _record_gold_outcome(locked, 'WIN', live)
                _gold_feed.pop(tf, None)
                _gold_cooldowns[tf] = now + _GOLD_COOLDOWN.get(tf, 60)
                _save_gold_feed()
                log.info(f"[Gold {tf}] TP HIT — WIN @ {live}")
                continue
            if direction == 'bearish' and live <= tp:
                _record_gold_outcome(locked, 'WIN', live)
                _gold_feed.pop(tf, None)
                _gold_cooldowns[tf] = now + _GOLD_COOLDOWN.get(tf, 60)
                _save_gold_feed()
                log.info(f"[Gold {tf}] TP HIT — WIN @ {live}")
                continue

        # Check SL hit
        if live and sl:
            if direction == 'bullish' and live <= sl:
                _record_gold_outcome(locked, 'LOSS', live)
                _gold_feed.pop(tf, None)
                _gold_cooldowns[tf] = now + _GOLD_COOLDOWN.get(tf, 60)
                _save_gold_feed()
                log.info(f"[Gold {tf}] SL HIT — LOSS @ {live}")
                continue
            if direction == 'bearish' and live >= sl:
                _record_gold_outcome(locked, 'LOSS', live)
                _gold_feed.pop(tf, None)
                _gold_cooldowns[tf] = now + _GOLD_COOLDOWN.get(tf, 60)
                _save_gold_feed()
                log.info(f"[Gold {tf}] SL HIT — LOSS @ {live}")
                continue

        # Backstop TTL — last resort if MT5 disconnected and missed a close
        if age > _GOLD_TTL.get(tf, 3600):
            _record_gold_outcome(locked, 'EXPIRED', live or 0)
            _gold_feed.pop(tf, None)
            _save_gold_feed()
            log.info(f"[Gold {tf}] TTL expired — EXPIRED")

    # ── Ingest new gold scalp signals ─────────────────────────────────────
    for tf, r in results.items():
        if not r.get('data'):
            continue
        data    = r['data']
        verdict = data.get('verdict', 'SKIP')
        # Only lock FIRE signals (price is at the OB right now, during a session)
        if verdict != 'FIRE':
            continue
        if not data.get('in_session'):
            continue
        if not data.get('entry') or not data.get('sl') or not data.get('tp'):
            continue
        rr = data.get('rr') or 0
        if rr < 1.5:
            continue

        # ── News + Macro stance gate ─────────────────────────────────────────────
        # 1. Active HIGH window (±2min of event) → always block
        # 2. Imminent HIGH event (< 15min away) → block (spike kills OBs instantly)
        # 3. AI stance contradicts signal direction → block (don't trade against macro)
        # 4. VOLATILE stance → block all directions (extreme unpredictability)
        try:
            from news_calendar import (is_news_active, get_next_event,
                                       generate_market_context, get_upcoming_events)
            signal_dir = data.get('direction', 'none')  # 'bullish' or 'bearish'

            # Gate 1: Active HIGH-impact window
            if is_news_active(tier='high'):
                log.info(f"[Gold GATE] {tf} {signal_dir} — HIGH news window active, skip lock")
                continue

            # Gate 2: Imminent HIGH event (15-min pre-freeze)
            nxt = get_next_event(tier='high')
            if nxt and nxt.get('seconds_until', 9999) < 300:
                log.info(f"[Gold GATE] {tf} {signal_dir} — '{nxt['title']}' fires in "
                         f"{nxt['seconds_until']//60}m, skip lock")
                continue

            # Gate 3+4: AI macro stance vs signal direction
            ai_ctx = generate_market_context(get_upcoming_events(tier='all'))
            stance = ai_ctx.get('gold_stance', 'clear')

            if stance == 'volatile':
                log.info(f"[Gold GATE] {tf} {signal_dir} — stance=VOLATILE, skip lock")
                continue
            if stance == 'bearish' and signal_dir == 'bullish':
                log.info(f"[Gold GATE] {tf} — BUY blocked by BEARISH macro stance")
                continue
            if stance == 'bullish' and signal_dir == 'bearish':
                log.info(f"[Gold GATE] {tf} — SELL blocked by BULLISH macro stance")
                continue
            # 'cautious' and 'clear' → pass through; existing price-action gates handle the rest
        except Exception as _ge:
            log.debug(f"[Gold GATE] news gate error (non-blocking): {_ge}")

        # Cooldown gate — simple 60s lockout after any close
        # Real quality filtering is handled by sweep requirement + M5 alignment.
        cd = _gold_cooldowns.get(tf, 0)
        if isinstance(cd, dict):
            cd = cd.get('expiry', 0)   # handle old dict format if loaded from disk
        if cd > now:
            continue

        # Already locked — validation already done above; skip
        if tf in _gold_feed:
            continue

        # ── Lock new gold signal ──────────────────────────────────────────
        _gold_feed[tf] = {
            'tf':              tf,
            'direction':       data.get('direction'),
            'swept_liquidity': data.get('swept_liquidity', False),
            'locked_trade': {
                'entry': data['entry'],
                'sl':    data['sl'],
                'tp':    data['tp'],
                'rr':    data['rr'],
            },
            'tp_distance':     data.get('tp_distance'),
            'atr':             data.get('atr'),
            'data':            data,
            'ts':              now,
        }
        _save_gold_feed()
        log.info(f"[Gold LOCKED] {tf} {data.get('direction')} "
                 f"entry={data['entry']} sl={data['sl']} tp={data['tp']} rr={data['rr']}")

    # ── Build feed response ───────────────────────────────────────────────
    feed = []
    for tf, locked in list(_gold_feed.items()):
        age_s = now - locked['ts']
        t     = locked.get('locked_trade', {})
        r     = results.get(tf, {})
        live  = (r.get('data') or {}).get('price')
        feed.append({
            'tf':              tf,
            'direction':       locked.get('direction'),
            'swept_liquidity': locked.get('swept_liquidity', False),
            'entry':           t.get('entry'),
            'sl':              t.get('sl'),
            'tp':              t.get('tp'),
            'rr':              t.get('rr'),
            'tp_distance':     locked.get('tp_distance'),
            'price':           live,
            'age_secs':        round(age_s),
            'age_mins':        round(age_s / 60, 1),
        })

    # Sort fastest TF first
    tf_order = {'M1': 0, 'M3': 1, 'M5': 2}
    feed.sort(key=lambda x: tf_order.get(x['tf'], 9))

    # ── Persistent WATCH management ───────────────────────────────────────────
    # WATCH signals are stored in _gold_watch so they survive multiple scan
    # cycles. A WATCH stays visible until: (a) price arrives → FIRE locks it,
    # (b) its TTL expires, or (c) a genuinely different setup replaces it.
    for tf in ('M1', 'M3', 'M5'):
        # If already locked as FIRE, remove any stale WATCH for this TF
        if tf in _gold_feed:
            _gold_watch.pop(tf, None)
            continue

        r       = results.get(tf, {})
        d       = (r.get('data') or {}) if r else {}
        verdict = d.get('verdict', 'SKIP')
        live    = d.get('price', 0)

        if verdict == 'WATCH' and d.get('entry') and d.get('sl') and d.get('tp') \
                and (d.get('rr') or 0) >= 1.5:
            existing   = _gold_watch.get(tf)
            new_entry  = d['entry']
            new_dir    = d.get('direction', '')
            ex_entry   = existing.get('entry', 0) if existing else 0
            ex_dir     = existing.get('direction', '') if existing else ''
            ex_age     = (now - existing.get('ts', 0)) if existing else 9999
            ex_ttl     = _GOLD_WATCH_TTL.get(tf, 1800)

            # When to REPLACE the existing WATCH with the new one:
            #   (a) No existing WATCH yet — store immediately
            #   (b) Direction reversed — market has flipped, old setup invalid
            #   (c) Entry differs by > $3 AND old WATCH is >50% of its TTL old
            #       → old card was stale, fresh OB is more relevant
            # When to KEEP the existing WATCH (just update price):
            #   — Same direction, entry similar (< $3 apart) OR still fresh
            #   → Don't overwrite an OB the user may have set a limit order on
            direction_flipped   = existing and ex_dir and ex_dir != new_dir
            entry_far_and_stale = abs(ex_entry - new_entry) > 3.0 and ex_age > ex_ttl * 0.5
            should_replace      = (not existing) or direction_flipped or entry_far_and_stale

            if not should_replace:
                # Keep existing — just refresh live price so violation check stays accurate
                _gold_watch[tf]['price']      = live
                _gold_watch[tf]['in_session'] = d.get('in_session', False)
            else:
                _gold_watch[tf] = {
                    'tf':              tf,
                    'direction':       new_dir,
                    'entry':           new_entry,
                    'sl':              d.get('sl'),
                    'tp':              d.get('tp'),
                    'rr':              d.get('rr'),
                    'price':           live,
                    'in_session':      d.get('in_session', False),
                    'candles_ago':     d.get('candles_ago'),
                    'disp_atr_mult':   d.get('disp_atr_mult'),
                    'ob':              d.get('ob'),
                    'swept_liquidity': d.get('swept_liquidity', False),
                    'ts':              now,
                }
        elif tf in _gold_watch:
            # Scanner returned SKIP but we have a stored WATCH — keep it alive
            # until TTL expires; just update live price
            if live:
                _gold_watch[tf]['price']      = live
                _gold_watch[tf]['in_session'] = d.get('in_session', False)

    # Expire old WATCH entries + warn on OB-violated setups
    for tf in list(_gold_watch.keys()):
        w   = _gold_watch[tf]
        age = now - w.get('ts', 0)

        # If already violated: keep visible for 5 min so users can cancel, then remove
        if w.get('violated'):
            if now - w.get('violated_at', now) > 300:   # 5 min grace period
                _gold_watch.pop(tf, None)
            continue

        if age > _GOLD_WATCH_TTL.get(tf, 300):
            _gold_watch.pop(tf, None)
            continue

        # OB violation check — if price blown through the SL zone the OB is broken.
        # DO NOT silently delete — users may have a limit order sitting at entry.
        # Instead: mark VIOLATED so the dashboard shows a red WARNING for 5 minutes.
        live_chk = w.get('price', 0)
        sl_chk   = w.get('sl', 0)
        dir_chk  = w.get('direction', '')
        if live_chk and sl_chk:
            ob_broken = (
                (dir_chk == 'bearish' and live_chk >= sl_chk) or
                (dir_chk == 'bullish' and live_chk <= sl_chk)
            )
            if ob_broken:
                log.info(f"[Gold WATCH {tf}] OB violated — live={live_chk} sl={sl_chk} "
                         f"{dir_chk} — WARNING shown for 5m then removed")
                _gold_watch[tf]['violated']    = True
                _gold_watch[tf]['violated_at'] = now

    # ── Conflict filter: higher TF wins ──────────────────────────────────────
    # If M5 and M1 (or M5 and M3) point in OPPOSITE directions, suppress the
    # lower TF. A market that looks bullish on M1 but bearish on M5 is just
    # noise inside a bigger bearish move — trading M1 against M5 is dangerous.
    m5_dir = _gold_watch.get('M5', {}).get('direction')
    m3_dir = _gold_watch.get('M3', {}).get('direction')
    m1_dir = _gold_watch.get('M1', {}).get('direction')

    def conflicts(dir_a, dir_b):
        return dir_a and dir_b and dir_a != dir_b

    suppress = set()
    if conflicts(m5_dir, m1_dir):
        suppress.add('M1')   # M5 overrides M1
    if conflicts(m5_dir, m3_dir):
        suppress.add('M3')   # M5 overrides M3
    if conflicts(m3_dir, m1_dir) and 'M3' not in suppress:
        suppress.add('M1')   # M3 overrides M1 when M5 absent

    # Build watching list from persistent store
    watching = []
    for tf, w in _gold_watch.items():
        if tf in _gold_feed:
            continue  # already FIRE-locked
        violated = w.get('violated', False)
        if tf in suppress and not violated:
            continue  # conflicting lower-TF signal — hide it (but ALWAYS show violated)
        age_s = now - w.get('ts', now)
        watching.append(dict(w, age_secs=round(age_s), conflicted=(tf in suppress)))

    # ── Gold context (news + OPEX + post-news OB window) ─────────────────────
    gold_ctx = {}
    try:
        from news_calendar import get_gold_context
        gold_ctx = get_gold_context()
    except Exception as e:
        log.debug(f"get_gold_context failed: {e}")

    return jsonify({
        'signals':       feed,
        'watching':      watching,
        'count':         len(feed),
        'gold_context':  gold_ctx,
    })


@app.route('/api/gold-history')
def gold_history_endpoint():
    """Gold scalper trade history with win/loss stats."""
    decided  = [t for t in _gold_history if t['outcome'] in ('WIN', 'LOSS')]
    wins     = sum(1 for t in decided if t['outcome'] == 'WIN')
    losses   = sum(1 for t in decided if t['outcome'] == 'LOSS')
    total_d  = wins + losses
    win_rate = round(wins / total_d * 100) if total_d > 0 else 0
    cur_streak = 0
    if decided:
        first = decided[0]['outcome']
        for t in decided:
            if t['outcome'] == first:
                cur_streak += 1
            else:
                break
    stats = {
        'total':       len(_gold_history),
        'wins':        wins,
        'losses':      losses,
        'expired':     sum(1 for t in _gold_history if t['outcome'] == 'EXPIRED'),
        'win_rate':    win_rate,
        'streak':      cur_streak,
        'streak_type': decided[0]['outcome'] if decided else None,
    }
    return jsonify({'history': _gold_history[:100], 'stats': stats})


# ── Level Gravity Feed API ────────────────────────────────────────────────────

@app.route('/api/gravity-feed')
def gravity_feed_endpoint():
    """
    Level Gravity live feed.
    Same lock-in pattern as /api/gold-feed:
      • FIRE signal locks entry until TP or SL is hit, or TTL expires.
      • Cooldown prevents re-entry immediately after a close.
      • Runs 24/7 on M1 — no session filter.
    """
    now         = time.time()
    tick        = mt5.symbol_info_tick(GOLD_SYMBOL)
    live        = round((tick.bid + tick.ask) / 2, 2) if tick else None
    feed        = []
    tf          = 'M1'

    # ── Check existing locked signal ──────────────────────────────────────────
    locked = _gravity_feed.get(tf)
    if locked:
        direction = locked.get('direction')
        tp  = locked.get('tp')
        sl  = locked.get('sl')
        age = now - locked.get('locked_ts', now)

        if live is not None:
            # TP hit
            if direction == 'BUY' and live >= tp:
                _record_gravity_outcome(locked, 'WIN', live)
                _gravity_feed.pop(tf, None)
                _gravity_cooldowns[tf]  = now + _GRAVITY_WIN_COOLDOWN   # 20s — kills fake pullbacks
                _gravity_last_close[tf] = {'direction': 'BUY', 'ts': now, 'outcome': 'WIN'}
                scanner.trigger_gravity_rescan(tf)   # force fresh entry price on next lock
                log.info(f"[Gravity {tf}] TP HIT — WIN @ {live}")
                locked = None
            elif direction == 'SELL' and live <= tp:
                _record_gravity_outcome(locked, 'WIN', live)
                _gravity_feed.pop(tf, None)
                _gravity_cooldowns[tf]  = now + _GRAVITY_WIN_COOLDOWN   # 20s — kills fake pullbacks
                _gravity_last_close[tf] = {'direction': 'SELL', 'ts': now, 'outcome': 'WIN'}
                scanner.trigger_gravity_rescan(tf)   # force fresh entry price on next lock
                log.info(f"[Gravity {tf}] TP HIT — WIN @ {live}")
                locked = None
            # SL hit
            elif direction == 'BUY' and live <= sl:
                _record_gravity_outcome(locked, 'LOSS', live)
                _gravity_feed.pop(tf, None)
                _gravity_cooldowns[tf]  = now + _GRAVITY_COOLDOWN       # 10s
                _gravity_last_close[tf] = {'direction': 'BUY', 'ts': now, 'outcome': 'LOSS'}
                scanner.trigger_gravity_rescan(tf)   # force fresh entry price on next lock
                log.info(f"[Gravity {tf}] SL HIT — LOSS @ {live}")
                locked = None
            elif direction == 'SELL' and live >= sl:
                _record_gravity_outcome(locked, 'LOSS', live)
                _gravity_feed.pop(tf, None)
                _gravity_cooldowns[tf]  = now + _GRAVITY_COOLDOWN       # 10s
                _gravity_last_close[tf] = {'direction': 'SELL', 'ts': now, 'outcome': 'LOSS'}
                scanner.trigger_gravity_rescan(tf)   # force fresh entry price on next lock
                log.info(f"[Gravity {tf}] SL HIT — LOSS @ {live}")
                locked = None

        if locked:
            feed.append({**locked, 'age': int(age), 'live': live})

    # ── Try to lock a new signal if no active trade ───────────────────────────
    if not locked:
        in_cooldown = now < _gravity_cooldowns.get(tf, 0)
        if not in_cooldown:
            try:
                from level_gravity import analyze_gravity_scalp
                data = analyze_gravity_scalp(tf)   # direct call — always fresh live entry price
            except Exception as _ge:
                log.warning(f"[Gravity] analyze_gravity_scalp failed: {_ge}")
                data = {}
        else:
            data = {}

        # ── Trend WARN alert (fires once per WARN window) ─────────────────────
        if data.get('trend_warn'):
            try:
                _send_gravity_warn(data.get('trend_dir', '?'))
            except Exception as _wa:
                log.warning(f"[GravityWarn] alert error: {_wa}")

        if not in_cooldown and data.get('verdict') == 'FIRE':
            entry           = data.get('entry')
            new_direction   = data.get('direction')
            momentum_score  = data.get('momentum_score', 3)   # 0-5 votes bullish (5-vote system)

            if entry:
                sig = {
                    'tf':             tf,
                    'strategy':       'GRAVITY',
                    'verdict':        'FIRE',
                    'direction':      new_direction,
                    'entry':          data['entry'],
                    'tp':             data['tp'],
                    'sl':             data['sl'],
                    'rr':             data['rr'],
                    'tp_dist':        data.get('tp_dist'),
                    'sl_dist':        data.get('sl_dist'),
                    'atr':            data.get('atr'),
                    'atr_tp':         data.get('atr_tp'),
                    'level_tp':       data.get('level_tp'),
                    'tp_capped':      data.get('tp_capped', False),
                    'level_high':     data.get('level_high'),
                    'level_low':      data.get('level_low'),
                    'momentum':       data.get('momentum'),
                    'momentum_score': momentum_score,
                    'bias':           data.get('bias'),
                    'tp1':            data.get('tp1'),
                    'spread':         data.get('spread'),
                    'live':           live,
                    'locked_ts':      now,
                }
                _gravity_feed[tf] = sig
                scanner.consume_gravity_fire(tf)   # mark signal consumed — stops it re-appearing
                feed.append({**sig, 'age': 0})
                log.info(f"[Gravity {tf}] LOCKED {sig['direction']} entry={sig['entry']} "
                         f"tp={sig['tp']} sl={sig['sl']} votes={momentum_score}/3")

    # ── Stats ─────────────────────────────────────────────────────────────────
    decided    = [t for t in _gravity_history if t['outcome'] in ('WIN', 'LOSS')]
    wins       = sum(1 for t in decided if t['outcome'] == 'WIN')
    losses     = sum(1 for t in decided if t['outcome'] == 'LOSS')
    total_d    = wins + losses
    win_rate   = round(wins / total_d * 100) if total_d > 0 else 0
    total_pts  = sum(t.get('pnl_pts', 0) for t in _gravity_history if t['outcome'] in ('WIN', 'LOSS'))

    try:
        from level_gravity import VERSION as LG_VERSION
    except Exception:
        LG_VERSION = 'unknown'

    return jsonify({
        'signals':        feed,
        'history':        _gravity_history[-20:],
        'engine_version': LG_VERSION,
        'stats': {
            'total':    len(_gravity_history),
            'wins':     wins,
            'losses':   losses,
            'win_rate': win_rate,
            'total_pts': round(total_pts, 2),
        },
        'live':      live,
        'cooldown_remaining': max(0, round(_gravity_cooldowns.get(tf, 0) - now)),
    })


# ── Internal Gravity Signal — for Gravity Bot ────────────────────────────────

@app.route('/internal/gravity-signal')
def internal_gravity_signal():
    """
    Internal endpoint polled by the Gravity Bot every few seconds.
    Returns the current locked FIRE signal with entry/tp/sl/direction,
    or has_signal=False when no trade is active.

    Secured with X-Bot-Secret header (matches ADMIN_SECRET env var).
    """
    _bot_secret = os.getenv('ADMIN_SECRET', '')
    if _bot_secret:
        if request.headers.get('X-Bot-Secret', '') != _bot_secret:
            return jsonify({'error': 'unauthorized'}), 401

    tf     = 'M1'
    locked = _gravity_feed.get(tf)

    if not locked:
        return jsonify({'has_signal': False, 'signal': None})

    return jsonify({
        'has_signal': True,
        'signal': {
            'tf':             tf,
            'direction':      locked.get('direction'),      # 'BUY' or 'SELL'
            'entry':          locked.get('entry'),
            'tp':             locked.get('tp'),
            'sl':             locked.get('sl'),
            'rr':             locked.get('rr'),
            'tp_dist':        locked.get('tp_dist'),
            'sl_dist':        locked.get('sl_dist'),
            'atr':            locked.get('atr'),
            'momentum_score': locked.get('momentum_score'),
            'signal_id':      str(locked.get('locked_ts', '')),  # unique per signal lock
        }
    })


# ── Candle Surfer API — dashboard feed ───────────────────────────────────────

@app.route('/api/candle-surfer-feed')
def candle_surfer_feed():
    decided  = [t for t in _surfer_history if t['outcome'] in ('WIN', 'LOSS')]
    wins     = sum(1 for t in decided if t['outcome'] == 'WIN')
    losses   = sum(1 for t in decided if t['outcome'] == 'LOSS')
    total_d  = wins + losses
    win_rate = round(wins / total_d * 100) if total_d > 0 else 0
    net_pts  = round(sum(t.get('pnl_pts', 0) for t in _surfer_history), 2)

    active = bool(_surfer_feed)
    signal = _surfer_feed.copy() if active else {}

    # Live unrealized P&L for active signal
    if active:
        try:
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(GOLD_SYMBOL)
            if tick:
                direction = signal.get('direction')
                entry     = signal.get('entry', 0)
                if direction == 'BUY':
                    signal['unrealized'] = round(tick.bid - entry - _SURFER_SPREAD, 2)
                    signal['live']       = round(tick.bid, 2)
                else:
                    signal['unrealized'] = round(entry - tick.ask - _SURFER_SPREAD, 2)
                    signal['live']       = round(tick.ask, 2)
        except Exception:
            pass

    return jsonify({
        'active'  : active,
        'signal'  : signal,
        'trades'  : total_d,
        'wins'    : wins,
        'losses'  : losses,
        'win_rate': win_rate,
        'net_pts' : net_pts,
        'history' : _surfer_history[:30],
    })


# ── Candle Surfer — internal bot endpoint ────────────────────────────────────

@app.route('/internal/candle-surfer-signal')
def internal_candle_surfer_signal():
    """Polled by candle_surfer_bot.py every second. Returns active signal or has_signal=False."""
    secret = os.getenv('ADMIN_SECRET', '')
    if secret and request.headers.get('X-Bot-Secret', '') != secret:
        return jsonify({'error': 'unauthorized'}), 401
    has = bool(_surfer_feed)
    return jsonify({'has_signal': has, 'signal': _surfer_feed.copy() if has else None})


# ── Lock Scalper — in-memory state ───────────────────────────────────────────
_lock_state: dict = {
    'active'     : False,
    'version'    : '',
    'current'    : None,       # current open straddle info
    'history'    : [],         # last 50 closed straddles
    'trades'     : 0,
    'wins'       : 0,
    'net_pnl'    : 0.0,
    'last_update': 0.0,
}


@app.route('/api/lock-scalper-feed')
def lock_scalper_feed():
    """Dashboard reads this every 2 seconds when Lock Scalper panel is open."""
    s       = _lock_state
    trades  = s.get('trades', 0)
    wins    = s.get('wins', 0)
    losses  = max(0, trades - wins)
    win_rate = round(wins / trades * 100) if trades > 0 else 0
    return jsonify({
        'active'  : s.get('active', False),
        'version' : s.get('version', ''),
        'current' : s.get('current'),
        'history' : s.get('history', [])[:30],
        'trades'  : trades,
        'wins'    : wins,
        'losses'  : losses,
        'win_rate': win_rate,
        'net_pnl' : round(s.get('net_pnl', 0.0), 2),
    })


@app.route('/api/ls/tick')
def ls_tick_endpoint():
    """Real-time XAUUSD tick for the Lock Scalper dashboard simulator.
    Returns bid/ask/spread/ATR from MT5, or source='unavailable' when offline."""
    try:
        if not _mt5_init():
            return jsonify({'source': 'unavailable'})
        tick = mt5.symbol_info_tick(GOLD_SYMBOL)
        if not tick:
            return jsonify({'source': 'unavailable'})
        bid    = round(float(tick.bid), 2)
        ask    = round(float(tick.ask), 2)
        spread = round(ask - bid, 2)
        # ATR(14) from M1 candles
        rates = mt5.copy_rates_from_pos(GOLD_SYMBOL, mt5.TIMEFRAME_M1, 0, 20)
        atr = 2.0
        if rates is not None and len(rates) >= 15:
            trs = []
            for i in range(1, len(rates)):
                h      = float(rates[i]['high'])
                l      = float(rates[i]['low'])
                c_prev = float(rates[i - 1]['close'])
                trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
            subset = trs[-14:] if len(trs) >= 14 else trs
            atr    = round(sum(subset) / len(subset), 2)
        return jsonify({
            'bid'   : bid,
            'ask'   : ask,
            'mid'   : round((bid + ask) / 2, 2),
            'spread': spread,
            'atr'   : atr,
            'source': 'mt5',
            'ts'    : time.time(),
        })
    except Exception:
        return jsonify({'source': 'unavailable'})


# ── Lock Scalper trade history (persisted to disk) ─────────────────────────────
_LS_HISTORY_FILE = os.path.join('data', 'ls_history.json')


def _ls_history_load() -> list:
    try:
        if os.path.exists(_LS_HISTORY_FILE):
            with open(_LS_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _ls_history_save(records: list):
    try:
        os.makedirs('data', exist_ok=True)
        with open(_LS_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(records[:200], f)  # cap at 200 records
    except Exception as e:
        log.warning(f'ls_history save failed: {e}')


@app.route('/api/ls/history', methods=['GET'])
def ls_history_get():
    """Return stored Lock Scalper trade history (newest first, max 200)."""
    return jsonify(_ls_history_load())


@app.route('/api/ls/history', methods=['POST'])
def ls_history_post():
    """Append one trade record to Lock Scalper history."""
    record = request.get_json()
    if not record:
        return jsonify({'ok': False}), 400
    records = _ls_history_load()
    records.insert(0, record)   # newest first
    _ls_history_save(records)
    return jsonify({'ok': True})


@app.route('/api/broker-status')
def broker_status_proxy():
    """Proxy /status from Autocycle AI Broker → dashboard can poll same origin."""
    try:
        import urllib.request
        with urllib.request.urlopen(f'{_BROKER_URL}/status', timeout=4) as r:
            return app.response_class(r.read(), mimetype='application/json')
    except Exception as e:
        return jsonify({'error': str(e), 'phase': 'OFFLINE'}), 200


@app.route('/api/broker-history')
def broker_history_proxy():
    """Proxy /history from Autocycle AI Broker."""
    limit = request.args.get('limit', '50')
    try:
        import urllib.request
        url = f'{_BROKER_URL}/history?limit={limit}'
        with urllib.request.urlopen(url, timeout=4) as r:
            return app.response_class(r.read(), mimetype='application/json')
    except Exception as e:
        return jsonify({'error': str(e), 'history': []}), 200


@app.route('/api/broker-chart')
def broker_chart():
    """
    Return last N M1 OHLCV candles for XAUUSD+ from MT5.
    Used by the live broker chart on the dashboard.
    """
    n = int(request.args.get('n', '80'))
    try:
        if not _mt5_init():
            return jsonify({'error': 'MT5 init failed', 'candles': []}), 200

        import MetaTrader5 as _mt5
        sym = GOLD_SYMBOL
        # Ensure symbol is active
        _mt5.symbol_select(sym, True)
        rates = _mt5.copy_rates_from_pos(sym, _mt5.TIMEFRAME_M1, 0, n)
        if rates is None or len(rates) == 0:
            return jsonify({'error': 'No data', 'candles': []}), 200

        candles = [
            {
                't': int(r['time']),      # UNIX timestamp
                'o': float(r['open']),
                'h': float(r['high']),
                'l': float(r['low']),
                'c': float(r['close']),
                'v': int(r['tick_volume']),
            }
            for r in rates
        ]
        return jsonify({'candles': candles, 'symbol': sym})
    except Exception as e:
        log.exception('broker-chart error')
        return jsonify({'error': str(e), 'candles': []}), 200


@app.route('/internal/lock-scalper-status', methods=['POST'])
def lock_scalper_status():
    """Called by lock_scalper_bot.py to report its current state."""
    global _lock_state
    payload = request.json or {}
    # Merge into state
    for key in ('active', 'version', 'current', 'trades', 'wins', 'net_pnl'):
        if key in payload:
            _lock_state[key] = payload[key]
    # Append to history if a completed straddle was reported
    if 'completed' in payload:
        entry = payload['completed']
        _lock_state['history'].insert(0, entry)
        _lock_state['history'] = _lock_state['history'][:50]
    _lock_state['last_update'] = time.time()
    return jsonify({'ok': True})


# ── News Calendar API ─────────────────────────────────────────────────────────

@app.route('/api/news-calendar')
def news_calendar_endpoint():
    """Returns ALL-tier upcoming events + current news window status."""
    try:
        from news_calendar import (get_upcoming_events, get_next_event,
                                    get_active_event, is_news_active, is_news_day,
                                    get_gold_context, generate_market_context)
        events      = get_upcoming_events(tier='all')   # HIGH + MEDIUM + LOW, next 24h
        next_ev     = get_next_event(tier='high')       # next HIGH impact for banner
        active_ev   = get_active_event()
        news_active = is_news_active(tier='high')
        news_day    = is_news_day()
        gold_ctx    = get_gold_context()
        ai_ctx      = generate_market_context(events)   # AI market intelligence
    except Exception as exc:
        log.warning(f"news_calendar_endpoint error: {exc}")
        events      = []
        next_ev     = None
        active_ev   = None
        news_active = False
        news_day    = False
        gold_ctx    = {}
        ai_ctx      = {}

    return jsonify({
        'events':       events,
        'next_event':   next_ev,
        'active_event': active_ev,
        'news_active':  news_active,
        'news_day':     news_day,
        'gold_context': gold_ctx,
        'ai_context':   ai_ctx,      # AI market intelligence card
    })


# ── News Sniper Feed API ───────────────────────────────────────────────────────

@app.route('/api/news-feed')
def news_feed_endpoint():
    """Lock, manage TTL, and return news sniper signals (FIRE + WATCH)."""
    now     = time.time()
    to_lock = scanner.get_news_sniper_results()

    # ── Lock signals from scanner ──────────────────────────────────────────────
    for result in to_lock:
        verdict = result.get('verdict')
        symbol  = result.get('symbol', '')
        tf      = result.get('tf', 'M5')
        key     = f"{symbol}_{tf}"

        if verdict == 'FIRE':
            if key in _news_feed:
                continue  # already locked as FIRE
            _news_feed[key] = {
                'symbol':       symbol,
                'tf':           tf,
                'direction':    result.get('direction', ''),
                'entry':        result.get('entry'),
                'sl':           result.get('sl'),
                'tp':           result.get('tp'),
                'rr':           result.get('rr'),
                'price_at_ob':  result.get('price_at_ob', False),
                'is_reversal':  result.get('is_reversal', False),
                'news_title':   result.get('news_title', ''),
                'news_country': result.get('news_country', ''),
                'atr':          result.get('atr'),
                'ob':           result.get('ob'),
                'ts':           now,
                'verdict':      'FIRE',
            }
            _news_watch.pop(key, None)   # remove from watch if it was promoted
            _save_news_feed()
            log.info(f"[NewsFeed] FIRE locked {key} {result.get('direction')} entry={result.get('entry')}")

        elif verdict == 'WATCH':
            if key in _news_feed or key in _news_watch:
                continue  # already handled
            _news_watch[key] = {
                'symbol':       symbol,
                'tf':           tf,
                'direction':    result.get('direction', ''),
                'entry':        result.get('entry'),
                'sl':           result.get('sl'),
                'tp':           result.get('tp'),
                'rr':           result.get('rr'),
                'is_reversal':  result.get('is_reversal', False),
                'news_title':   result.get('news_title', ''),
                'news_country': result.get('news_country', ''),
                'atr':          result.get('atr', 1.0),
                'ob':           result.get('ob', {}),
                'ts':           now,
                'verdict':      'WATCH',
            }
            log.info(f"[NewsFeed] WATCH stored {key} {result.get('direction')} entry={result.get('entry')} — monitoring for price arrival at OB")

    # ── Monitor WATCH items: auto-promote when price reaches OB ──────────────
    watch_expired = []
    watch_out     = []

    for key, watched in list(_news_watch.items()):
        tf   = watched.get('tf', 'M5')
        age  = now - watched.get('ts', 0)
        ttl  = _NEWS_WATCH_TTL.get(tf, 300)
        sym  = watched['symbol']
        sl   = watched.get('sl') or 0
        dirn = watched.get('direction', '')
        ob   = watched.get('ob') or {}
        atr  = watched.get('atr') or 1.0

        live = _current_price(sym)

        if age > ttl:
            watch_expired.append(key)
            log.info(f"[NewsFeed] WATCH {key} expired — price never reached OB")
            continue

        if live:
            # OB violated — SL level breached, zone no longer valid
            if dirn == 'bullish' and live <= sl:
                watch_expired.append(key)
                log.info(f"[NewsFeed] WATCH {key} OB violated (live={live} <= sl={sl})")
                continue
            elif dirn == 'bearish' and live >= sl:
                watch_expired.append(key)
                log.info(f"[NewsFeed] WATCH {key} OB violated (live={live} >= sl={sl})")
                continue

            # Price arrived at OB entry zone → promote to FIRE
            if ob and key not in _news_feed:
                buf         = atr * 0.35
                price_at_ob = (ob.get('low', 0) - buf) <= live <= (ob.get('high', 0) + buf)
                if price_at_ob:
                    _news_feed[key] = {**watched, 'verdict': 'FIRE',
                                       'price_at_ob': True, 'ts': now}
                    watch_expired.append(key)
                    _save_news_feed()
                    log.info(f"[NewsFeed] WATCH→FIRE {key} {dirn} — live={live} reached OB entry={watched.get('entry')}")
                    continue

        watch_out.append({**watched, 'age_s': int(age), 'ttl_s': ttl, 'live_price': live})

    for key in watch_expired:
        _news_watch.pop(key, None)

    # ── Manage FIRE feed: TTL + outcome checking ───────────────────────────────
    expired_keys = []
    feed_out     = []

    for key, locked in list(_news_feed.items()):
        tf    = locked.get('tf', 'M5')
        age   = now - locked.get('ts', 0)
        ttl   = _NEWS_TTL.get(tf, 720)
        sym   = locked['symbol']
        entry = locked.get('entry') or 0
        sl    = locked.get('sl')    or 0
        tp    = locked.get('tp')    or 0
        dirn  = locked.get('direction', '')

        live = _current_price(sym)

        if live:
            if dirn == 'bullish':
                if live >= tp:
                    _record_news_outcome(locked, 'WIN', live)
                    expired_keys.append(key)
                    continue
                elif live <= sl:
                    _record_news_outcome(locked, 'LOSS', live)
                    expired_keys.append(key)
                    continue
            elif dirn == 'bearish':
                if live <= tp:
                    _record_news_outcome(locked, 'WIN', live)
                    expired_keys.append(key)
                    continue
                elif live >= sl:
                    _record_news_outcome(locked, 'LOSS', live)
                    expired_keys.append(key)
                    continue

        if age > ttl:
            _record_news_outcome(locked, 'EXPIRED', live or entry)
            expired_keys.append(key)
            continue

        feed_out.append({**locked, 'age_s': int(age), 'ttl_s': ttl, 'live_price': live})

    for key in expired_keys:
        _news_feed.pop(key, None)
    if expired_keys:
        _save_news_feed()

    return jsonify({
        'feed':        feed_out,
        'count':       len(feed_out),
        'watch':       watch_out,
        'watch_count': len(watch_out),
    })


# ── News Sniper History API ────────────────────────────────────────────────────

@app.route('/api/news-history')
def news_history_endpoint():
    decided  = [t for t in _news_history if t['outcome'] in ('WIN', 'LOSS')]
    wins     = sum(1 for t in decided if t['outcome'] == 'WIN')
    losses   = len(decided) - wins
    win_rate = round(wins / len(decided) * 100, 1) if decided else 0

    cur_streak = 0
    if decided:
        last = decided[0]['outcome']
        for t in decided:
            if t['outcome'] == last:
                cur_streak += 1
            else:
                break

    stats = {
        'total':       len(_news_history),
        'wins':        wins,
        'losses':      losses,
        'expired':     sum(1 for t in _news_history if t['outcome'] == 'EXPIRED'),
        'win_rate':    win_rate,
        'streak':      cur_streak,
        'streak_type': decided[0]['outcome'] if decided else None,
    }
    return jsonify({'history': _news_history[:100], 'stats': stats})


# ── Signal Notify — Telegram push ─────────────────────────────────────────────

def _send_gravity_warn(trend_dir: str) -> None:
    """Push a ⚠️ trend exhaustion warning to the Telegram channel (fires once per WARN window)."""
    import os, urllib.request, urllib.error, json as _json
    tg_token   = os.getenv('TELEGRAM_BOT_TOKEN', '')
    tg_channel = os.getenv('TELEGRAM_CHANNEL_ID', '')
    if not tg_token or not tg_channel:
        return
    arrow = '📉' if trend_dir == 'DOWN' else '📈'
    opp   = 'BUY' if trend_dir == 'DOWN' else 'SELL'
    msg = (
        f"⚠️ <b>TREND EXHAUSTING — {arrow} {trend_dir}</b>\n\n"
        f"The {trend_dir}trend is losing steam.\n"
        f"Trend trades paused for ~60 seconds.\n"
        f"🎯 Prepare for <b>{opp}</b> reversal entry.\n\n"
        f"<i>5-vote will fire the reversal signal when ready.</i>"
    )
    try:
        payload = _json.dumps({'chat_id': tg_channel, 'text': msg, 'parse_mode': 'HTML'}).encode()
        req     = urllib.request.Request(
            f'https://api.telegram.org/bot{tg_token}/sendMessage',
            data=payload, headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=8)
        log.info(f"[GravityWarn] ⚠️ Trend exhaustion alert sent — dir={trend_dir}")
    except Exception as _we:
        log.warning(f"[GravityWarn] Failed to send warn alert: {_we}")


@app.route('/api/notify-signal', methods=['POST'])
def notify_signal_endpoint():
    """Send a signal alert to the Telegram channel from the dashboard notify button."""
    import os, urllib.request, urllib.error

    tg_token   = os.getenv('TELEGRAM_BOT_TOKEN', '')
    tg_channel = os.getenv('TELEGRAM_CHANNEL_ID', '')
    if not tg_token or not tg_channel:
        return jsonify({'ok': False, 'error': 'Telegram not configured on server'}), 400

    d      = request.get_json() or {}
    symbol = d.get('symbol', GOLD_SYMBOL)
    tf     = d.get('tf', '')
    dirn   = d.get('direction', 'bullish')
    entry  = d.get('entry')
    sl     = d.get('sl')
    tp     = d.get('tp')
    rr     = d.get('rr', '—')
    source = d.get('source', 'Signal')   # 'Gold Scalper', 'News Sniper', 'Signal'

    arrow    = '📈' if dirn == 'bullish' else '📉'
    dir_text = 'BUY' if dirn == 'bullish' else 'SELL'

    def fp(v):
        try: return f'{float(v):.4f}' if v else '—'
        except: return str(v or '—')

    msg = (
        f"{arrow} <b>AutoCycle AI — {source}</b>\n\n"
        f"<b>{dir_text} {symbol}</b> · {tf}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 Entry: <b>{fp(entry)}</b>\n"
        f"🛑 SL:    {fp(sl)}\n"
        f"✅ TP:    {fp(tp)}\n"
        f"📊 R:R:   {rr}:1\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<i>Sent from AutoCycle AI dashboard</i>"
    )

    try:
        payload = json.dumps({'chat_id': tg_channel, 'text': msg,
                              'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{tg_token}/sendMessage',
            data=payload, headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=10)
        log.info(f"[Notify] Pushed {dir_text} {symbol} {tf} to Telegram channel")
        return jsonify({'ok': True})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.warning(f"[Notify] Telegram HTTP error: {e.code} {body}")
        return jsonify({'ok': False, 'error': f'Telegram error {e.code}'}), 500
    except Exception as e:
        log.warning(f"[Notify] Telegram send failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/internal/scalp-signal', methods=['GET'])
def internal_scalp_signal():
    """
    Internal endpoint for the scalper bot.
    Returns the best current FIRE signal from the gold scalper scanner.
    Protected by X-Bot-Secret header matching ADMIN_SECRET env var.
    """
    import os
    secret = os.getenv('ADMIN_SECRET', '')
    if secret and request.headers.get('X-Bot-Secret') != secret:
        return jsonify({'error': 'unauthorized'}), 401

    results = scanner.get_gold_results()   # {tf: {data, ts, error}, ...}

    fire_signals = []
    now = time.time()
    for tf, result in results.items():
        if not result or result.get('error'):
            continue
        data = result.get('data') or {}
        if data.get('verdict') != 'FIRE':
            continue
        direction = data.get('direction', 'none')
        if direction == 'none' or not data.get('entry'):
            continue
        fire_signals.append({
            'tf':        tf,
            'direction': direction,       # 'bullish' or 'bearish'
            'entry':     data.get('entry'),
            'sl':        data.get('sl'),
            'tp':        data.get('tp'),
            'rr':        data.get('rr', 1.5),
            'atr':       data.get('atr'),
            'swept':     data.get('swept_liquidity', False),
            'dxy_bias':  data.get('dxy_bias', 'neutral'),
            'ts':        result['ts'],
            # Unique ID for deduplication in the bot
            'signal_id': f"{tf}_{direction}_{data.get('entry', 0):.2f}",
        })

    # ── Macro news gate — same logic as /api/gold-feed lock gate ─────────────
    # The scalper bot must not execute trades that go against the macro backdrop.
    # This protects all subscribers from counter-trend news casualties.
    gate_reason = None
    stance      = 'clear'
    try:
        from news_calendar import (is_news_active, get_next_event,
                                   generate_market_context, get_upcoming_events)
        if is_news_active(tier='high'):
            gate_reason = 'HIGH-impact news window active'
        else:
            nxt = get_next_event(tier='high')
            if nxt and nxt.get('seconds_until', 9999) < 300:
                gate_reason = f"HIGH event imminent ({nxt['seconds_until']//60}m): {nxt.get('title','?')}"

        if not gate_reason:
            ai_ctx = generate_market_context(get_upcoming_events(tier='all'))
            stance = ai_ctx.get('gold_stance', 'clear')
            if stance == 'volatile':
                gate_reason = 'macro stance=VOLATILE'

        if gate_reason:
            # Block ALL signals during news windows / volatile stance
            log.info(f"[BotSignal BLOCKED-ALL] {gate_reason}")
            fire_signals = []
        elif stance not in ('clear', 'cautious'):
            # Directional filter: suppress trades that contradict the macro
            allowed = []
            for sig in fire_signals:
                d = sig['direction']
                if stance == 'bearish' and d == 'bullish':
                    log.info(f"[BotSignal BLOCKED] {sig['tf']} BUY — macro=BEARISH")
                    continue
                if stance == 'bullish' and d == 'bearish':
                    log.info(f"[BotSignal BLOCKED] {sig['tf']} SELL — macro=BULLISH")
                    continue
                allowed.append(sig)
            fire_signals = allowed
    except Exception as _ge:
        log.debug(f"[BotSignal] news gate error (non-blocking): {_ge}")

    # Priority: swept OB > regular OB; within same tier pick the freshest
    best = None
    for sig in fire_signals:
        if best is None:
            best = sig
        elif sig['swept'] and not best['swept']:
            best = sig
        elif sig['swept'] == best['swept'] and sig['ts'] > best['ts']:
            best = sig

    return jsonify({
        'has_signal':   best is not None,
        'signal':       best,
        'all_fire':     fire_signals,
        'macro_stance': stance,
        'gate_reason':  gate_reason,     # lets the bot log WHY it got no signal
        'server_ts':    now,
    })


# ── Background Gravity Monitor ────────────────────────────────────────────────
# Checks TP/SL/TTL every 2 seconds, 24/7, independent of dashboard polling.
# Without this, detection only happened when a browser had the dashboard open.

def _gravity_monitor_loop():
    """
    Background thread: monitors the locked gravity trade against live MT5 price.
    Runs every 2 seconds so TP/SL are caught within seconds, not minutes.
    """
    import threading
    tf = 'M1'
    log.info("[GravityMonitor] Background monitor started — checking every 2s")
    while True:
        try:
            now    = time.time()
            locked = _gravity_feed.get(tf)
            if locked:
                tick = mt5.symbol_info_tick(GOLD_SYMBOL)
                live = round((tick.bid + tick.ask) / 2, 2) if tick else None
                if live is not None:
                    direction = locked.get('direction')
                    tp        = locked.get('tp')
                    sl        = locked.get('sl')
                    age       = now - locked.get('locked_ts', now)

                    # TP hit
                    if direction == 'BUY' and live >= tp:
                        _record_gravity_outcome(locked, 'WIN', live)
                        _gravity_feed.pop(tf, None)
                        _gravity_cooldowns[tf]  = now + _GRAVITY_WIN_COOLDOWN
                        _gravity_last_close[tf] = {'direction': 'BUY', 'ts': now, 'outcome': 'WIN'}
                        scanner.trigger_gravity_rescan(tf)
                        log.info(f"[GravityMonitor] TP HIT — WIN @ {live}")
                    elif direction == 'SELL' and live <= tp:
                        _record_gravity_outcome(locked, 'WIN', live)
                        _gravity_feed.pop(tf, None)
                        _gravity_cooldowns[tf]  = now + _GRAVITY_WIN_COOLDOWN
                        _gravity_last_close[tf] = {'direction': 'SELL', 'ts': now, 'outcome': 'WIN'}
                        scanner.trigger_gravity_rescan(tf)
                        log.info(f"[GravityMonitor] TP HIT — WIN @ {live}")
                    # SL hit
                    elif direction == 'BUY' and live <= sl:
                        _record_gravity_outcome(locked, 'LOSS', live)
                        _gravity_feed.pop(tf, None)
                        _gravity_cooldowns[tf]  = now + _GRAVITY_COOLDOWN
                        _gravity_last_close[tf] = {'direction': 'BUY', 'ts': now, 'outcome': 'LOSS'}
                        scanner.trigger_gravity_rescan(tf)
                        log.info(f"[GravityMonitor] SL HIT — LOSS @ {live}")
                    elif direction == 'SELL' and live >= sl:
                        _record_gravity_outcome(locked, 'LOSS', live)
                        _gravity_feed.pop(tf, None)
                        _gravity_cooldowns[tf]  = now + _GRAVITY_COOLDOWN
                        _gravity_last_close[tf] = {'direction': 'SELL', 'ts': now, 'outcome': 'LOSS'}
                        scanner.trigger_gravity_rescan(tf)
                        log.info(f"[GravityMonitor] SL HIT — LOSS @ {live}")
        except Exception as _e:
            log.warning(f"[GravityMonitor] error: {_e}")
        time.sleep(2)


def _start_gravity_monitor():
    import threading
    t = threading.Thread(target=_gravity_monitor_loop, daemon=True, name='GravityMonitor')
    t.start()


if __name__ == '__main__':
    import os
    print("\n" + "=" * 50)
    print("  AutoCycle Trend Analyzer is starting...")
    print("  Make sure MT5 is open and logged in.")
    print("=" * 50 + "\n")
    _load_history()    # restore trade history from disk
    _load_feed()       # restore locked signals — survive restarts without losing active trades
    _load_gold_data()     # restore gold scalper feed + history
    _load_news_data()     # restore news sniper feed + history
    _load_gravity_history()  # restore gravity scalper history
    _start_gravity_monitor()  # background TP/SL monitor — runs 24/7, no browser needed
    _load_surfer_history()   # restore candle surfer history
    _start_candle_surfer()   # tick-level candle surfing monitor
    scanner.start()   # start background multi-pair scanner (includes gold scalper + news sniper loops)

    # Production mode (waitress) vs development mode
    if os.environ.get('PRODUCTION'):
        from waitress import serve
        port = int(os.environ.get('PORT', 5000))
        print(f"  Running in PRODUCTION mode on port {port}")
        serve(app, host='0.0.0.0', port=port, threads=8)
    else:
        print("  Running in DEV mode at http://localhost:5000")
        app.run(debug=True, port=5000, use_reloader=False)
