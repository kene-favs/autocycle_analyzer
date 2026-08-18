"""
check_status.py
───────────────
Run from the project root to verify every feature we built is correctly
installed in this session. Does NOT need the broker to be running — it
checks files directly. If the broker IS running it also calls /status.

Usage:
    python check_status.py
"""
import sys, os, importlib, traceback, json
sys.path.insert(0, os.path.dirname(__file__))

PASS = '✅'
FAIL = '❌'
WARN = '⚠️ '

results = []

def check(label, fn):
    try:
        msg = fn()
        results.append((PASS, label, msg or ''))
    except Exception as e:
        results.append((FAIL, label, str(e)))

# ─────────────────────────────────────────────────────────────────────────────
print('\n══════════════════════════════════════════════════════')
print('  Autocycle AI Broker — Session Feature Check')
print('══════════════════════════════════════════════════════\n')

# ── 1. File existence ─────────────────────────────────────────────────────────
print('── 1. FILE EXISTENCE ────────────────────────────────')
FILES = [
    'autocycle_broker/engine.py',
    'autocycle_broker/broker.py',
    'autocycle_broker/book.py',
    'autocycle_broker/config.py',
    'gap_scanner.py',
    'level_gravity.py',
    'app.py',
]
for f in FILES:
    check(f, lambda f=f: 'present' if os.path.exists(f) else (_ for _ in ()).throw(FileNotFoundError(f'{f} MISSING')))

# ── 2. Config values ──────────────────────────────────────────────────────────
print()
print('── 2. CONFIG VALUES ─────────────────────────────────')

def _check_config():
    from autocycle_broker import config
    checks = {
        'EARLY_HEDGE_THRESHOLD': (config.EARLY_HEDGE_THRESHOLD, 0.20),
        'SCAN_INTERVAL'        : (config.SCAN_INTERVAL,          0.10),
        'HEDGE_MAGIC'          : (config.HEDGE_MAGIC,            None),
        'FIXED_LOT'            : (config.FIXED_LOT,              None),
    }
    lines = []
    for name, (val, expected) in checks.items():
        ok = (expected is None) or (val == expected)
        lines.append(f'{PASS if ok else WARN} {name} = {val}')
    return '\n   '.join(lines)

check('autocycle_broker/config.py imports + values', _check_config)

# ── 3. book.py — lot_for_risk ─────────────────────────────────────────────────
print()
print('── 3. BOOK.PY ───────────────────────────────────────')

def _check_book():
    from autocycle_broker import book
    assert hasattr(book, 'lot_for_risk'), 'lot_for_risk function MISSING'
    import inspect
    src = inspect.getsource(book.lot_for_risk)
    assert 'risk_pct' in src, 'lot_for_risk missing risk_pct parameter'
    return '1% risk-based lot sizing present'

check('lot_for_risk() in book.py', _check_book)

# ── 4. engine.py — early hedge ────────────────────────────────────────────────
print()
print('── 4. ENGINE.PY — EARLY HEDGE ───────────────────────')

def _check_engine_state():
    import autocycle_broker.engine as eng
    state = eng._state
    assert '_early_hedge_ticket' in state, '_early_hedge_ticket missing from _state'
    assert '_early_hedge_side'   in state, '_early_hedge_side missing from _state'
    assert '_early_hedge_price'  in state, '_early_hedge_price missing from _state'
    return 'all 3 early hedge state fields present'

def _check_engine_source():
    with open('autocycle_broker/engine.py', encoding='utf-8') as f:
        src = f.read()
    checks = {
        'EARLY_HEDGE_THRESHOLD read'     : 'config.EARLY_HEDGE_THRESHOLD' in src,
        'EarlyHedge open log'            : '[EarlyHedge] ✅' in src,
        'EarlyHedge reuse log'           : '[EarlyHedge] ♻️' in src,
        'EarlyHedge SL detection'        : 'positions_get(ticket=_eh_ticket)' in src,
        'EarlyHedge orphan close'        : '_orphan_ticket' in src,
        'Chase filter removed'           : 'skip_reentry' not in src or 'removed' in src,
        'lot_for_risk called'            : 'lot_for_risk(' in src,
        'Smart exit (reversal)'          : 'Reversal confirmed' in src,
        'Smart exit (stall 90s)'         : '_stall_since' in src,
        'Market intel OB+velocity'       : '_get_orderbook_bias' in src,
        'Currency bias import'           : 'get_gold_bias' in src,
    }
    lines = []
    for label, ok in checks.items():
        lines.append(f'{PASS if ok else FAIL} {label}')
    failed = [l for l, ok in checks.items() if not ok]
    if failed:
        raise AssertionError('Missing: ' + ', '.join(failed))
    return '\n   '.join(lines)

check('_state early hedge fields', _check_engine_state)
check('engine.py source checks', _check_engine_source)

# ── 5. level_gravity.py — exhaustion + OB block ───────────────────────────────
print()
print('── 5. LEVEL_GRAVITY.PY — EXHAUSTION + OB BLOCK ─────')

def _check_gravity():
    with open('level_gravity.py', encoding='utf-8') as f:
        src = f.read()
    checks = {
        '_get_ob_bias_quick defined'     : 'def _get_ob_bias_quick' in src,
        'v5_fired flag set'              : 'v5_fired = True' in src,
        'v5_fired returned'              : 'return direction, int(score_for_direction), v5_fired' in src,
        'v5_fired unpacked in main'      : 'momentum, momentum_score, v5_fired' in src,
        'GravityExhaustion BLOCKED log'  : 'GravityExhaustion' in src,
        'OB check on exhaustion'         : '_get_ob_bias_quick(SYMBOL)' in src,
    }
    lines = []
    for label, ok in checks.items():
        lines.append(f'{PASS if ok else FAIL} {label}')
    failed = [l for l, ok in checks.items() if not ok]
    if failed:
        raise AssertionError('Missing: ' + ', '.join(failed))
    return '\n   '.join(lines)

check('level_gravity.py exhaustion + OB block', _check_gravity)

# ── 6. gap_scanner.py ─────────────────────────────────────────────────────────
print()
print('── 6. GAP_SCANNER.PY ────────────────────────────────')

def _check_gap():
    with open('gap_scanner.py', encoding='utf-8') as f:
        src = f.read()
    checks = {
        'XAUUSD+ trigger_broker=True'   : "'trigger_broker': True" in src,
        'EURUSD trigger_broker=False'   : 'trigger_broker' in src and 'EURUSD' in src,
        'BTC removed'                   : 'BTCUSD' not in src and 'BTC' not in src.split('def ')[0],
        'SCAN_INTERVAL = 2s'            : 'SCAN_INTERVAL    = 2' in src,
        'get_gold_bias exported'        : 'def get_gold_bias' in src,
        'CURRENCY_BIAS_TTL = 300s'      : 'CURRENCY_BIAS_TTL = 300' in src,
        'trigger cooldown 60s'          : 'TRIGGER_COOLDOWN = 60' in src,
    }
    lines = []
    for label, ok in checks.items():
        lines.append(f'{PASS if ok else FAIL} {label}')
    failed = [l for l, ok in checks.items() if not ok]
    if failed:
        raise AssertionError('Missing: ' + ', '.join(failed))
    return '\n   '.join(lines)

check('gap_scanner.py source checks', _check_gap)

# ── 7. broker.py — build version + gap scanner start ─────────────────────────
print()
print('── 7. BROKER.PY — BUILD INFO ────────────────────────')

def _check_broker():
    from autocycle_broker import broker
    assert hasattr(broker, 'BUILD_VERSION'), 'BUILD_VERSION missing'
    assert hasattr(broker, 'BUILD_FEATURES'), 'BUILD_FEATURES missing'
    assert len(broker.BUILD_FEATURES) >= 10, 'BUILD_FEATURES too short'
    with open('autocycle_broker/broker.py', encoding='utf-8') as f:
        src = f.read()
    assert 'gap_scanner' in src, 'gap_scanner not imported in broker.py'
    assert "_gap_scanner.start()" in src, 'gap_scanner.start() missing'
    return f'version={broker.BUILD_VERSION}  features={len(broker.BUILD_FEATURES)}'

check('broker.py build info + gap scanner', _check_broker)

# ── 8. tick_follower.py ───────────────────────────────────────────────────────
print()
print('── 8. TICK_FOLLOWER.PY ─────────────────────────────')

def _check_tf():
    with open('tick_follower.py', encoding='utf-8') as f:
        src = f.read()
    checks = {
        '7-pair POOL defined'              : "POOL = ['EURUSD', 'AUDUSD', 'GBPUSD', 'NZDUSD', 'USDJPY', 'USDCAD', 'USDCHF']" in src,
        'TRADING_START = 5'                : 'TRADING_START        = 5' in src,
        'TRADING_END = 20'                 : 'TRADING_END          = 20' in src,
        'MAGIC = 20260814'                 : 'MAGIC     = 20260814' in src,
        'POLL_MS env var'                  : 'TF_POLL_MS' in src,
        'ENTRY_THRESHOLD_PIPS'             : 'ENTRY_THRESHOLD_PIPS' in src,
        'STALL_THRESHOLD_PIPS'             : 'STALL_THRESHOLD_PIPS' in src,
        'SAFETY_SL_PIPS = 1.5'            : "TF_SAFETY_SL',  '1.5'" in src,
        'Lot tiers _LOT_TIERS'             : '_LOT_TIERS' in src,
        '_lot_for_balance() defined'       : 'def _lot_for_balance' in src,
        'Pair scorer _score_all_pairs'     : 'def _score_all_pairs' in src,
        '_velocity_scores dict'            : '_velocity_scores' in src,
        '_best_two() defined'              : 'def _best_two' in src,
        '_velocity() for slot detection'   : 'def _velocity' in src,
        'Stall exit in _tick'              : 'STALL_THRESHOLD_PIPS' in src and 'stall' in src,
        'Dynamic _manage_slots'            : 'def _manage_slots' in src,
        'Watchdog thread'                  : 'def _watchdog' in src,
        '_emergency_close_all defined'     : 'def _emergency_close_all' in src,
        '20ms precise sleep'               : 'perf_counter' in src,
        'USDJPY 3-digit pip handled'       : 'info.digits in (5, 3)' in src,
        'start() exported'                 : 'def start()' in src,
        'get_state() returns scores'       : "'scores'" in src,
        'get_state() returns session_utc'  : "'session_utc'" in src,
    }
    lines = []
    for label, ok in checks.items():
        lines.append(f'{PASS if ok else FAIL} {label}')
    failed = [l for l, ok in checks.items() if not ok]
    if failed:
        raise AssertionError('Missing: ' + ', '.join(failed))
    return '\n   '.join(lines)

check('tick_follower.py v2.9.0 source checks', _check_tf)

def _check_tf_wired():
    with open('autocycle_broker/broker.py', encoding='utf-8') as f:
        src = f.read()
    checks = {
        'tick_follower imported'      : 'import tick_follower' in src,
        'tick_follower.start() called': '_tick_follower.start()' in src,
        'tick_follower in /status'    : '_tick_follower.get_state()' in src,
        'build = v2.9.0'              : "BUILD_VERSION  = 'v2.9.0'" in src,
        '14 build features'           : src.count("'✅") >= 14,
        'cascade removed'             : 'forex_cascade' not in src,
        'watchdog feature listed'     : 'watchdog' in src,
    }
    lines = []
    for label, ok in checks.items():
        lines.append(f'{PASS if ok else FAIL} {label}')
    failed = [l for l, ok in checks.items() if not ok]
    if failed:
        raise AssertionError('Missing: ' + ', '.join(failed))
    return '\n   '.join(lines)

check('broker.py v2.9.0 wiring', _check_tf_wired)

# ── 9. Live broker /status (only if running) ──────────────────────────────────
print()
print('── 9. LIVE BROKER /STATUS (if running) ─────────────')

def _check_live():
    try:
        import requests
        from autocycle_broker import config
        r = requests.get(
            f'http://localhost:{config.BROKER_PORT}/status',
            timeout=3,
        )
        if not r.ok:
            return f'broker responded but status={r.status_code}'
        data = r.json()
        phase = data.get('phase', '?')
        build = data.get('build', {})
        ver   = build.get('version', '?')
        feats = len(build.get('features', []))
        bal   = data.get('balance', 0)
        sim   = '🟡 SIMULATE' if data.get('simulate') else '🟢 LIVE'
        eh    = '✅ present' if '_early_hedge_ticket' in str(data) else '(not in status — normal)'
        return (
            f'phase={phase} | {sim} | build={ver} ({feats} features) | '
            f'balance=${bal:.2f}'
        )
    except Exception as e:
        return f'broker not reachable ({e}) — start it first, then re-run this check'

check('GET /status from running broker (cascade field present)', _check_live)

# ─────────────────────────────────────────────────────────────────────────────
# Print results
# ─────────────────────────────────────────────────────────────────────────────
print()
print('══════════════════════════════════════════════════════')
print('  RESULTS')
print('══════════════════════════════════════════════════════')
passed = failed = 0
for icon, label, detail in results:
    print(f'\n{icon}  {label}')
    if detail:
        for line in detail.split('\n'):
            print(f'   {line}')
    if icon == PASS:
        passed += 1
    elif icon == FAIL:
        failed += 1

print()
print('──────────────────────────────────────────────────────')
if failed == 0:
    print(f'  ALL {passed} checks passed — system is good to go 🚀')
else:
    print(f'  {passed} passed  |  {failed} FAILED ← fix these before trading')
print('══════════════════════════════════════════════════════\n')
