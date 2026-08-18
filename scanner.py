"""
AutoCycle Background Scanner
=============================
Two scan loops run in parallel (both are sequential MT5 calls — MT5 is NOT
thread-safe, so a single global lock serialises every MT5 call):

  1. FAST LOOP  — M5 only, cycles every ~25 s. Catches breakouts at the
                  very beginning of a 5-minute candle before the move is gone.

  2. FULL LOOP  — M15 + H1 across all symbols, cycles every ~60 s.

Both loops share _results and _lock.
"""

import os
import threading
import time
import logging

log = logging.getLogger(__name__)

# Gold symbol — read from env so Vantage (XAUUSD+) and others (XAUUSD) both work
_GOLD_SYMBOL = os.getenv('MT5_SYMBOL', 'XAUUSD+')

# Symbols scanned automatically (most liquid / most traded)
SCAN_SYMBOLS = [
    _GOLD_SYMBOL,                                  # Gold (broker-aware)
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF',       # Majors
    'AUDUSD', 'NZDUSD', 'USDCAD',
    'EURJPY', 'GBPJPY', 'EURGBP',                 # Crosses
    'AUDJPY', 'GBPCHF', 'EURCHF',                 # More crosses (liquid)
    'GBPCAD', 'EURAUD', 'CADCHF', 'NZDJPY',       # Additional crosses
    'GBPAUD', 'EURCAD',                            # Extra liquid crosses
    'XAGUSD',                                      # Silver
    'US30', 'UK100',                               # Indices (NAS100 not available on FBS)
    'XTIUSD',                                      # Oil
]

SCAN_TIMEFRAMES = ['M5', 'M15', 'M30', 'H1']   # shown in scanner table

# Fast loop — M5 + M15 (catch short-TF sweeps as early as possible)
M5_INTERVAL  = 3    # seconds gap between fast cycles — cycle itself takes ~10s, so total ≈13s
M5_DELAY     = 0.3  # delay between MT5 calls in fast loop

# Full loop — M30 + H1 (slower candles but we want fast detection)
FULL_INTERVAL = 5    # seconds gap between full cycles — cycle itself takes ~20s, so total ≈25s
FULL_DELAY    = 0.6  # delay between MT5 calls

# Single lock shared across both loops — MT5 is NOT thread-safe
_mt5_lock = threading.Lock()

_results: dict  = {}   # { "XAUUSD_M15": {symbol, tf, data, ts, error} }
_lock           = threading.Lock()
_running        = False
_current_symbol = 'Not started'
_progress       = 0
_last_cycle_ts  = 0.0


def get_results():
    """Thread-safe snapshot of scanner results."""
    with _lock:
        return dict(_results), _current_symbol, _progress, _last_cycle_ts


def _store(symbol, tf, data=None, error=None):
    key = f"{symbol}_{tf}"
    with _lock:
        if data is not None:
            _results[key] = {'symbol': symbol, 'tf': tf, 'data': data,
                             'ts': time.time(), 'error': None}
        elif key not in _results:
            _results[key] = {'symbol': symbol, 'tf': tf, 'data': None,
                             'ts': time.time(), 'error': str(error)}


# ── Fast M5 loop ───────────────────────────────────────────────────────────────

def _m5_loop():
    """Scans M5 + M15 across all symbols every ~15s. Catches sweeps early."""
    global _running
    from trend_analyzer import analyze

    while _running:
        for symbol in SCAN_SYMBOLS:
            for tf in ('M5', 'M15'):   # both short TFs in the fast loop
                if not _running:
                    return
                with _mt5_lock:
                    try:
                        data = analyze(symbol, tf)
                        _store(symbol, tf, data=data)
                        log.debug(f"FastScan {tf} {symbol}")
                    except Exception as exc:
                        log.warning(f"FastScan {tf} {symbol}: {exc}")
                        _store(symbol, tf, error=exc)
                time.sleep(M5_DELAY)

        for _ in range(M5_INTERVAL):
            if not _running:
                return
            time.sleep(1)


# ── Full M15 + H1 loop ─────────────────────────────────────────────────────────

def _full_loop():
    """Scans M15 and H1 across all symbols. Updates scanner table."""
    global _running, _current_symbol, _progress, _last_cycle_ts
    from trend_analyzer import analyze

    tfs   = ['M30', 'H1']   # M5+M15 handled by fast loop; full loop covers M30+H1
    total = len(SCAN_SYMBOLS) * len(tfs)

    while _running:
        done = 0
        for symbol in SCAN_SYMBOLS:
            for tf in tfs:
                if not _running:
                    return
                _current_symbol = f"{symbol} {tf}"
                _progress       = int(done / total * 100)
                with _mt5_lock:
                    try:
                        data = analyze(symbol, tf)
                        _store(symbol, tf, data=data)
                        log.debug(f"FullScan {symbol} {tf}")
                    except Exception as exc:
                        log.warning(f"FullScan {symbol} {tf}: {exc}")
                        _store(symbol, tf, error=exc)
                done += 1
                time.sleep(FULL_DELAY)

        _progress       = 100
        _current_symbol = 'Idle'
        _last_cycle_ts  = time.time()

        for _ in range(FULL_INTERVAL):
            if not _running:
                return
            time.sleep(1)


# ── Gold Scalper Loop ──────────────────────────────────────────────────────────

GOLD_TFS = ['M1', 'M3', 'M5']   # fast scalp TFs only

# Per-TF scan intervals:
# M1 → 1s: the entry trigger (price-at-OB check) uses live tick — catching it 4s sooner matters.
#           Candle structure is cached in gold_scalper._get_candles() with a 5s TTL,
#           so the MT5 candle fetch and pandas computation still only run every 5s.
# M3 → 3s: balance between speed and MT5 load; structure cached 8s
# M5 → 10s: M5 moves slowly; no need to check faster
_GOLD_SCAN_INTERVALS = {'M1': 1, 'M3': 3, 'M5': 10}

_gold_results: dict = {}   # { "M5": {tf, data, ts, error} }
_gold_lock          = threading.Lock()


def get_gold_results():
    """Thread-safe snapshot of gold scalper results."""
    with _gold_lock:
        return dict(_gold_results)


def _gold_loop():
    """
    Scans XAUUSD M1/M3/M5 with per-TF intervals.
    M1 and M3 refresh every 5 s; M5 every 10 s.
    """
    global _running
    from gold_scalper import analyze_gold_scalp

    last_scan    = {tf: 0.0 for tf in GOLD_TFS}
    last_logged  = {}   # {tf: (verdict, direction, entry)} — suppress duplicate log lines

    while _running:
        now = time.time()
        for tf in GOLD_TFS:
            if not _running:
                return
            interval = _GOLD_SCAN_INTERVALS.get(tf, 10)
            if now - last_scan[tf] < interval:
                continue
            with _mt5_lock:
                try:
                    data = analyze_gold_scalp(tf)
                    with _gold_lock:
                        _gold_results[tf] = {'tf': tf, 'data': data,
                                             'ts': time.time(), 'error': None}
                    v      = data.get('verdict', 'SKIP')
                    reason = data.get('skip_reason', '')
                    sig    = (v, data.get('direction'), data.get('entry'))
                    if v in ('FIRE', 'WATCH'):
                        # Only log when something actually changed — verdict, direction, or entry.
                        # Suppresses the 60×/min spam from the same candle being re-detected.
                        if last_logged.get(tf) != sig:
                            log.info(f"[Gold {tf}] {v} {data.get('direction')} "
                                     f"entry={data.get('entry')} rr={data.get('rr')}")
                            last_logged[tf] = sig
                    else:
                        # Log SKIP at INFO so the user can see the scanner is alive
                        # and understand why no setup was found. De-duplicated like FIRE/WATCH.
                        if last_logged.get(tf) != sig:
                            skip_msg = f"[Gold {tf}] SKIP"
                            if reason:
                                skip_msg += f" — {reason}"
                            log.info(skip_msg)
                            last_logged[tf] = sig
                except Exception as exc:
                    log.warning(f"GoldScan {tf}: {exc}")
                    with _gold_lock:
                        _gold_results[tf] = {'tf': tf, 'data': None,
                                             'ts': time.time(), 'error': str(exc)}
            _gravity_last_scan[tf] = time.time()

        time.sleep(1)  # wake each second to check which TF is due


# ── Level Gravity Loop ─────────────────────────────────────────────────────────

GRAVITY_TFS             = ['M1']   # Gravity runs on M1 only — fires every 15s
_GRAVITY_SCAN_INTERVAL  = 5        # seconds between gravity scans — fast enough to catch HMA5 flips quickly

_gravity_results:   dict = {}   # { "M1": {tf, data, ts, error} } — latest scan result
_gravity_last_fire: dict = {}   # { "M1": {data, ts, consumed} }  — persists FIRE until app.py locks it
_gravity_lock            = threading.Lock()

FIRE_PERSIST_SEC = 30  # keep a FIRE result visible to app.py for this long after scan


def consume_gravity_fire(tf: str):
    """
    Called by app.py AFTER successfully locking a gravity signal.
    Marks the pending FIRE as consumed so it won't be re-offered.
    """
    fire = _gravity_last_fire.get(tf)
    if fire:
        fire['consumed'] = True


def get_gravity_results():
    """
    Thread-safe snapshot of gravity results.
    If the latest scan is SKIP but there's a recent unconsumed FIRE,
    return the FIRE instead — prevents the dashboard missing a signal
    due to timing between the 15s scan cycle and poll rate.
    """
    with _gravity_lock:
        result = dict(_gravity_results)

    for tf, res in result.items():
        if res.get('data', {}).get('verdict') != 'FIRE':
            fire = _gravity_last_fire.get(tf)
            if (fire
                    and not fire.get('consumed')
                    and time.time() - fire.get('ts', 0) < FIRE_PERSIST_SEC):
                result[tf] = {
                    'tf': tf, 'data': fire['data'],
                    'ts': fire['ts'], 'error': None,
                }
    return result


_gravity_last_scan: dict = {tf: 0.0 for tf in GRAVITY_TFS}   # module-level so app.py can reset it


def trigger_gravity_rescan(tf: str):
    """
    Force an immediate re-scan of the given TF on the next loop tick.
    Called by app.py when a signal is blocked by the re-entry rule —
    so the opposite direction gets evaluated right away instead of
    waiting up to 15 s for the next scheduled scan.
    """
    _gravity_last_scan[tf] = 0.0


def _gravity_loop():
    """
    Level Gravity Scalper loop.
    Scans M1 every 15 s. Fires toward the nearest $1 level when momentum agrees.
    Runs 24/7 — no session filter.
    """
    global _running
    from level_gravity import analyze_gravity_scalp, VERSION as LG_VERSION
    log.info(f"[Gravity] Engine loaded — {LG_VERSION}")
    last_logged   = {}   # {tf: sig_tuple} — suppress duplicate logs
    last_heartbeat = 0.0  # timestamp of last heartbeat log

    while _running:
        now = time.time()
        for tf in GRAVITY_TFS:
            if not _running:
                return
            if now - _gravity_last_scan[tf] < _GRAVITY_SCAN_INTERVAL:
                continue
            with _mt5_lock:
                try:
                    data = analyze_gravity_scalp(tf)
                    with _gravity_lock:
                        _gravity_results[tf] = {'tf': tf, 'data': data,
                                                'ts': time.time(), 'error': None}
                    v      = data.get('verdict', 'SKIP')
                    reason = data.get('skip_reason', '')
                    sig    = (v, data.get('direction'), data.get('entry'))

                    if v == 'FIRE':
                        # Record unconsumed FIRE — persists in get_gravity_results()
                        # even if the next 15s scan returns SKIP
                        _gravity_last_fire[tf] = {
                            'data': data, 'ts': time.time(), 'consumed': False
                        }
                        if last_logged.get(tf) != sig:
                            log.info(f"[Gravity {tf}] FIRE {data.get('direction')} "
                                     f"entry={data.get('entry')} tp={data.get('tp')} "
                                     f"sl={data.get('sl')} rr={data.get('rr')} "
                                     f"levels=[{data.get('level_low')}–{data.get('level_high')}]")
                            last_logged[tf] = sig
                    else:
                        if last_logged.get(tf) != sig:
                            msg = f"[Gravity {tf}] SKIP"
                            if reason:
                                msg += f" — {reason}"
                            log.info(msg)
                            last_logged[tf] = sig
                        elif now - last_heartbeat > 60:
                            # Heartbeat every 60s even if skip reason unchanged — confirms loop is alive
                            log.info(f"[Gravity {tf}] scanning... (live=${data.get('live', '?')}) — {reason}")
                            last_heartbeat = now
                except Exception as exc:
                    log.warning(f"GravityScan {tf}: {exc}")
                    with _gravity_lock:
                        _gravity_results[tf] = {'tf': tf, 'data': None,
                                                'ts': time.time(), 'error': str(exc)}
            _gravity_last_scan[tf] = time.time()

        time.sleep(1)


# ── News Sniper Loop ───────────────────────────────────────────────────────────

NEWS_CHECK_INTERVAL = 10    # check every 10 seconds
NEWS_SNIPER_DELAY   = 15    # seconds after event fires before sniper runs — catch the initial spike
NEWS_SNIPER_WINDOW  = 300   # seconds after event to keep trying (5 min)

_news_sniper_results: list = []   # list of recent sniper result dicts
_news_sniper_lock          = threading.Lock()
_fired_events: set         = set()  # event timestamps already processed


def get_news_sniper_results() -> list:
    """Thread-safe snapshot of news sniper results."""
    with _news_sniper_lock:
        return list(_news_sniper_results)


def _news_loop():
    """
    Monitors for fired high-impact news events.
    Waits NEWS_SNIPER_DELAY seconds after an event, then runs the sniper.
    Processes each event only once.
    """
    global _running
    from news_calendar import events_fired_recently, force_refresh
    from news_sniper   import run_sniper_for_event

    # Warm up calendar on start
    try:
        force_refresh()
    except Exception:
        pass

    while _running:
        try:
            # Events that fired between 75s and 300s ago are in the sniper window
            recent = events_fired_recently(since_seconds=NEWS_SNIPER_WINDOW)
            for ev in recent:
                age = ev.get('seconds_ago', 0)
                ev_ts = int(ev['ts'])

                # Only process once the 75s settling window has passed
                if age < NEWS_SNIPER_DELAY:
                    continue

                # Don't process the same event twice
                if ev_ts in _fired_events:
                    continue

                log.info(f"NewsSniper: activating for '{ev['title']}' ({age}s ago)")
                _fired_events.add(ev_ts)

                # Run sniper for this event (inside MT5 lock)
                with _mt5_lock:
                    try:
                        results = run_sniper_for_event(ev)
                    except Exception as exc:
                        log.warning(f"NewsSniper run failed: {exc}")
                        results = []

                if results:
                    with _news_sniper_lock:
                        _news_sniper_results.clear()
                        _news_sniper_results.extend(results)
                    log.info(f"NewsSniper: {len(results)} signals found for '{ev['title']}'")

        except Exception as exc:
            log.warning(f"NewsLoop error: {exc}")

        for _ in range(NEWS_CHECK_INTERVAL):
            if not _running:
                return
            time.sleep(1)


# ── Start / Stop ───────────────────────────────────────────────────────────────

def start():
    global _running
    if _running:
        return
    _running = True

    threading.Thread(target=_m5_loop,      daemon=True, name='AutoCycle-M5Fast').start()
    threading.Thread(target=_full_loop,    daemon=True, name='AutoCycle-Full').start()
    threading.Thread(target=_gravity_loop, daemon=True, name='AutoCycle-Gravity').start()
    threading.Thread(target=_news_loop,    daemon=True, name='AutoCycle-News').start()

    _gold_enabled = os.getenv('GOLD_SCALPER_ENABLED', 'true').lower() == 'true'
    if _gold_enabled:
        threading.Thread(target=_gold_loop, daemon=True, name='AutoCycle-Gold').start()
        log.info("AutoCycle scanner started — M5 fast + M15/H1 full + Gold OB + Level Gravity + News sniper — %d symbols",
                 len(SCAN_SYMBOLS))
    else:
        log.info("AutoCycle scanner started — Gold OB disabled (GOLD_SCALPER_ENABLED=false) — %d symbols",
                 len(SCAN_SYMBOLS))


def stop():
    global _running
    _running = False
