"""
autocycle_broker/broker.py
──────────────────────────
FastAPI service — what your lock_scalper_bot.py calls.

Start:  python -m autocycle_broker
Or:     uvicorn autocycle_broker.broker:app --host 0.0.0.0 --port 8001

Endpoints
─────────
POST /open              → open a new BUY+SELL cycle (bot → broker)
GET  /status            → current phase, mid, cycle snapshot
GET  /positions         → open internal positions for current cycle
GET  /balance           → internal broker balance
GET  /history?limit=50  → last N completed cycles
GET  /health            → liveness probe
"""
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import book, config, engine

# ─── Build info ──────────────────────────────────────────────────────────────
BUILD_VERSION  = 'v2.9.0'
BUILD_DATE     = '2026-08-15'
BUILD_FEATURES = [
    '✅ High-frequency design: chase filter removed, re-entry restriction removed',
    '✅ Entry gate: extreme spread anomaly only (4x threshold — flash crash protection)',
    '✅ Entry gate: news filter (PRE_NEWS + chaos window)',
    '✅ Entry context: OB + tick velocity logged every open (not blocking)',
    '✅ Entry context: currency bias from EURUSD/GBPUSD gaps (logged, gravity wins)',
    '✅ Mid-trade smart exit: OB flip + velocity HIGH = instant close on reversal',
    '✅ Mid-trade smart exit: stall detection (OB neutral + velocity dead 90s)',
    '✅ Mid-trade OB + velocity check: every 1 second',
    '✅ 1% risk-based lot sizing per trade',
    '✅ Gap scanner: Gold auto-trigger every 2s',
    '✅ Gap scanner: EURUSD/GBPUSD context alerts (no broker trigger)',
    '✅ Tick Follower v2.9.0: 7-pair pool, dynamic best-2 selection, 05:00-20:00 UTC',
    '✅ Tick Follower: balance-scaled lot tiers ($50→0.05 … $8k+→4.00+)',
    '✅ Tick Follower: watchdog thread — auto-restart on crash + emergency close',
]

# Gap scanner lives outside the package — add parent dir to path if needed
import sys as _sys, os as _os
_parent = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)
try:
    import gap_scanner as _gap_scanner
except ImportError:
    _gap_scanner = None

try:
    import range_break as _tick_follower        # Range Breakout — primary strategy
except ImportError:
    try:
        import tick_follower as _tick_follower
    except ImportError:
        _tick_follower = None

_bracket_scalper  = None  # paused
_pulse_scalper    = None  # paused
_momentum_scalper = None  # paused

# ─── Logging ────────────────────────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/autocycle_broker.log', encoding='utf-8'),
    ],
)
log = logging.getLogger('AutocycleBroker')


# ─── Startup ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    book.init_db()
    engine.start_engine()
    if _gap_scanner:
        _gap_scanner.start()
        log.info('[GapScanner] Started — scanning Gold, BTC, EURUSD, GBPUSD for price gaps')
    if _tick_follower:
        _tick_follower.start()
        log.info('[RB] Started — Rolling Range Breakout Scalper v1.0.0: 7-pair pool, 10pip TP, 07:00-18:00 UTC')
    # bracket and pulse paused — range_break only
    log.info(
        f'Autocycle AI Broker listening on port {config.BROKER_PORT} | '
        f'symbol={config.SYMBOL} | server={config.MT5_SERVER}'
        + (' | *** SIMULATE MODE — no real orders ***' if config.SIMULATE else '')
    )
    yield
    log.info('Broker shutting down')


# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = 'Autocycle AI Broker',
    description = 'Personal broker middleware — internal BUY+SELL matching at mid price',
    version     = '1.0.0',
    lifespan    = lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins  = ['*'],
    allow_methods  = ['*'],
    allow_headers  = ['*'],
)


# ─── Auth helper ────────────────────────────────────────────────────────────
def _auth(secret: str):
    if config.BROKER_SECRET and secret != config.BROKER_SECRET:
        raise HTTPException(status_code=401, detail='Invalid broker secret')


# ─── Models ──────────────────────────────────────────────────────────────────
class OpenRequest(BaseModel):
    # Bot can supply a pre-computed ATR; if omitted the broker measures it.
    atr: float | None = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get('/health')
def health():
    """Liveness probe — returns 200 as long as the service is up."""
    return {'status': 'ok', 'phase': engine.get_state()['phase']}


@app.get('/status')
def status():
    """
    Full broker status — what the bot polls to detect phase changes.
    """
    st    = engine.get_state()
    cycle = book.get_current_cycle()
    mt5   = engine._mt5_account_info()
    bal   = mt5.get('balance', book.get_balance())   # real MT5 balance
    return {
        'phase'            : st['phase'],
        'mid'              : st['mid'],
        'atr'              : st['atr'],
        'cycle_id'         : st['cycle_id'],
        'sl_dist'          : st['sl_dist'],
        'tp_extra'         : st['tp_extra'],
        'sl_fired_side'    : st.get('sl_fired_side'),
        'sl_price'         : st.get('sl_price', 0.0),
        'tp_target'        : st.get('tp_target', 0.0),
        'cooldown_until'   : st['cooldown_until'],
        'balance'          : bal,
        'cycle'            : cycle,
        'error'            : st.get('error'),
        'simulate'         : config.SIMULATE,
        'active_symbol'    : st.get('active_symbol', config.SYMBOL),
        'schedule_enabled' : config.ENABLE_SCHEDULE,
        'gravity'          : {
            'verdict'   : engine._gravity_cache.get('verdict', 'SKIP'),
            'direction' : engine._gravity_cache.get('direction'),
            'score'     : engine._gravity_cache.get('score', 0),
            'updated_at': engine._gravity_cache.get('updated_at', 0),
        },
        'mt5_account'      : mt5,
        'build'            : {
            'version' : BUILD_VERSION,
            'date'    : BUILD_DATE,
            'features': BUILD_FEATURES,
        },
        'tick_follower'    : _tick_follower.get_state() if _tick_follower else None,
        'bracket_scalper'  : _bracket_scalper.get_state() if _bracket_scalper else None,
    }


@app.post('/open')
def open_cycle(
    req    : OpenRequest,
    secret : str = Header(default='', alias='X-Broker-Secret'),
):
    """
    Open a new BUY+SELL cycle.
    Broker must be in IDLE state. Returns cycle details.
    """
    _auth(secret)
    st = engine.get_state()
    if st['phase'] != 'IDLE':
        raise HTTPException(
            status_code=409,
            detail=f'Broker not IDLE — current phase: {st["phase"]}'
        )
    result = engine.trigger_open_cycle()
    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@app.get('/balance')
def balance():
    return {'balance': book.get_balance()}


@app.get('/positions')
def positions():
    st  = engine.get_state()
    cid = st.get('cycle_id')
    if not cid:
        return {'positions': []}
    return {'positions': book.get_open_positions(cid)}


@app.get('/history')
def history(limit: int = Query(default=50, ge=1, le=500)):
    return {'history': book.get_history(limit)}


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    uvicorn.run(
        'autocycle_broker.broker:app',
        host    = '0.0.0.0',
        port    = config.BROKER_PORT,
        reload  = False,
        workers = 1,
    )
