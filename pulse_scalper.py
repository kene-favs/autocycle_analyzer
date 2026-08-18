"""
pulse_scalper.py  —  Pulse Scalper  v1.0.0
═══════════════════════════════════════════
THE IDEA
────────
Enter on FIRST momentum signal — no confirmation, no waiting.
Exit the INSTANT price reverses from its peak.
Re-enter in 100ms. Always hunting the next move.

ENTRY
─────
Look at last 20 ticks (~0.4 seconds of raw price data).
≥ 60% ticked UP   → BUY immediately.
≥ 60% ticked DOWN → SELL immediately.
< 60% either way  → wait for next tick.

EXIT  — reversal detection (NO fixed stop loss)
─────────────────────────────────────────────────
Track the BEST price seen since entry (peak for BUY, trough for SELL).
The moment price pulls back REVERSAL_PIPS from that peak → close.

Example (BUY):
  Entry  @ 1.10000
  Peak   → 1.10030  (+0.30 pip)
  Pulls back to 1.10010  (0.20 pip from peak)
  → Close at 1.10010 = +0.10 pip net after commission = +$0.02

Example (wrong direction):
  Entry @ 1.10000, price drops to 1.09980 immediately
  Peak = entry = 1.10000, pullback = 0.20 pip
  → Close = -0.20 pip - commission = -$0.10

Safety server-side SL: 2.0 pip (protects against disconnect or spike).
Safety server-side TP: 10.0 pip (let big moves ride to their natural end).

MATH  (0.01 lot, EURUSD, $0.08 round-trip commission)
──────────────────────────────────────────────────────
  Big win  — move runs 2.0 pip then reversal exit:  +1.80 pip − $0.08 = +$0.10
  Small win — move runs 0.5 pip then reversal exit: +0.30 pip − $0.08 = −$0.05
  Loss     — wrong direction, exits at 0.20 pip:   −0.20 pip − $0.08 = −$0.10

  During London/NY, real moves are 1-5 pip. Reversal detection rides them.
  During quiet periods, small whipsaws lose tiny. The session hours avoid those.

SESSION : 05:00-21:00 UTC  (London open → NY close)
POOL    : EURUSD · AUDUSD · GBPUSD · NZDUSD · USDJPY · USDCAD · USDCHF
SLOTS   : 2 hottest pairs simultaneously
MAGIC   : 20260818  (different from range_break/bracket_scalper — no interference)

Env overrides
─────────────
  PS_SESSION_START   5      UTC hour session opens
  PS_SESSION_END    21      UTC hour session closes
  PS_POLL_MS        20      poll interval (ms)
  PS_REENTRY_MS    100      ms after close before re-entry
  PS_ENTRY_TICKS    20      how many recent ticks to read for direction
  PS_ENTRY_BIAS     0.60    fraction of ticks in same direction to trigger
  PS_REVERSAL       0.20    pips of pullback from peak → close
  PS_SAFETY_TP      10.0    server-side hard TP (pip)
  PS_SAFETY_SL       2.0    server-side hard SL (pip)
  PS_MIN_HOLD        5      minimum ticks before reversal exit allowed
"""

import os
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
POOL           = ['EURUSD', 'AUDUSD', 'GBPUSD', 'NZDUSD', 'USDJPY', 'USDCAD', 'USDCHF']
TRADING_START  = int(os.getenv('PS_SESSION_START', '5'))
TRADING_END    = int(os.getenv('PS_SESSION_END',   '21'))
POLL_MS        = int(os.getenv('PS_POLL_MS',       '20'))
REENTRY_MS     = int(os.getenv('PS_REENTRY_MS',    '30000'))  # 30 seconds between re-entries
ENTRY_TICKS    = int(os.getenv('PS_ENTRY_TICKS',   '20'))
ENTRY_BIAS     = float(os.getenv('PS_ENTRY_BIAS',  '0.65'))
REVERSAL_PIPS  = float(os.getenv('PS_REVERSAL',    '0.20'))
SAFETY_TP      = float(os.getenv('PS_SAFETY_TP',   '5.0'))
SAFETY_SL      = float(os.getenv('PS_SAFETY_SL',   '2.0'))
MIN_HOLD_TICKS = int(os.getenv('PS_MIN_HOLD',      '150'))   # 3 seconds minimum hold

MAGIC     = 20260818
DEVIATION = 20
LOT       = 0.01

_PIP = {
    'USDJPY': 0.01,
    'USDCAD': 0.0001,
    'USDCHF': 0.0001,
    'EURUSD': 0.0001,
    'GBPUSD': 0.0001,
    'AUDUSD': 0.0001,
    'NZDUSD': 0.0001,
}

def _pip(sym):
    return _PIP.get(sym, 0.0001)

# ─── MT5 ─────────────────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    _HAS_MT5 = True
except ImportError:
    mt5      = None
    _HAS_MT5 = False

# ─── Tick buffers ─────────────────────────────────────────────────────────────
_tick_buf = {s: deque(maxlen=200) for s in POOL}

def _feed_ticks():
    """Pull latest tick for every symbol in pool."""
    if not _HAS_MT5:
        return
    for sym in POOL:
        t = mt5.symbol_info_tick(sym)
        if t:
            mid = (t.ask + t.bid) / 2.0
            _tick_buf[sym].append((mid, t.time_msc / 1000.0))

def _get_bias(sym):
    """Return 'BUY', 'SELL', or None based on last ENTRY_TICKS ticks."""
    buf = list(_tick_buf[sym])
    if len(buf) < ENTRY_TICKS + 1:
        return None
    recent = buf[-ENTRY_TICKS:]
    up = sum(1 for i in range(1, len(recent)) if recent[i][0] > recent[i-1][0])
    dn = sum(1 for i in range(1, len(recent)) if recent[i][0] < recent[i-1][0])
    total = up + dn
    if total == 0:
        return None
    if up / total >= ENTRY_BIAS:
        return 'BUY'
    if dn / total >= ENTRY_BIAS:
        return 'SELL'
    return None

def _select_pairs():
    """Pick 2 pairs with the most recent tick activity."""
    now    = time.time()
    scores = []
    for sym in POOL:
        buf = _tick_buf[sym]
        if not buf:
            continue
        scores.append((sym, now - buf[-1][1]))
    scores.sort(key=lambda x: x[1])
    return [s for s, _ in scores[:2]]

# ─── Slot ─────────────────────────────────────────────────────────────────────
def _new_slot(sym):
    return {
        'symbol'      : sym,
        'active'      : False,
        'side'        : None,
        'ticket'      : None,
        'open_price'  : 0.0,
        'peak_price'  : 0.0,   # best price seen since entry
        'hold_ticks'  : 0,     # ticks elapsed since entry
        'close_after' : 0.0,   # epoch: earliest re-entry allowed
    }

# ─── Order helpers ────────────────────────────────────────────────────────────
def _open_order(sym, side):
    if not _HAS_MT5:
        return None
    info = mt5.symbol_info(sym)
    if not info or not info.visible:
        mt5.symbol_select(sym, True)
        info = mt5.symbol_info(sym)
    if not info:
        return None
    tick = mt5.symbol_info_tick(sym)
    if not tick:
        return None
    p       = _pip(sym)
    price   = tick.ask if side == 'BUY' else tick.bid
    sl      = round(price - SAFETY_SL * p, info.digits) if side == 'BUY' \
              else round(price + SAFETY_SL * p, info.digits)
    tp      = round(price + SAFETY_TP * p, info.digits) if side == 'BUY' \
              else round(price - SAFETY_TP * p, info.digits)
    req = {
        'action'      : mt5.TRADE_ACTION_DEAL,
        'symbol'      : sym,
        'volume'      : LOT,
        'type'        : mt5.ORDER_TYPE_BUY if side == 'BUY' else mt5.ORDER_TYPE_SELL,
        'price'       : price,
        'sl'          : sl,
        'tp'          : tp,
        'deviation'   : DEVIATION,
        'magic'       : MAGIC,
        'comment'     : 'pulse_scalper',
        'type_time'   : mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f'[PS] open  {sym} {side} @ {price:.5f}  ticket={res.order}')
        return res.order
    log.warning(f'[PS] open fail {sym} {side} retcode={res.retcode if res else "None"}')
    return None

def _close_order(slot):
    if not _HAS_MT5 or not slot['ticket']:
        return
    sym    = slot['symbol']
    side   = slot['side']
    info   = mt5.symbol_info(sym)
    tick   = mt5.symbol_info_tick(sym)
    if not tick or not info:
        return
    close_price = tick.bid if side == 'BUY' else tick.ask
    req = {
        'action'      : mt5.TRADE_ACTION_DEAL,
        'symbol'      : sym,
        'volume'      : LOT,
        'type'        : mt5.ORDER_TYPE_SELL if side == 'BUY' else mt5.ORDER_TYPE_BUY,
        'position'    : slot['ticket'],
        'price'       : close_price,
        'deviation'   : DEVIATION,
        'magic'       : MAGIC,
        'comment'     : 'pulse_close',
        'type_time'   : mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        p    = _pip(sym)
        pips = (close_price - slot['open_price']) / p if side == 'BUY' \
               else (slot['open_price'] - close_price) / p
        log.info(f'[PS] close {sym} {side} @ {close_price:.5f}  pips={pips:+.2f}  ticket={slot["ticket"]}')
    else:
        log.warning(f'[PS] close fail {sym} retcode={res.retcode if res else "None"}')

# ─── Main loop ────────────────────────────────────────────────────────────────
_slots      = {}
_slots_lock = threading.Lock()
_running    = False
_thread     = None
_wd_thread  = None

def _loop():
    log.info('[PS] Main loop running')
    while _running:
        now  = time.time()
        hour = datetime.now(timezone.utc).hour
        in_session = TRADING_START <= hour < TRADING_END

        _feed_ticks()

        if not in_session:
            time.sleep(POLL_MS / 1000.0)
            continue

        with _slots_lock:
            # ── Manage existing active positions ──────────────────────────
            for sym, slot in _slots.items():
                if not slot['active']:
                    continue
                if not _HAS_MT5:
                    continue
                tick = mt5.symbol_info_tick(sym)
                if not tick:
                    continue

                p   = _pip(sym)
                cur = tick.bid if slot['side'] == 'BUY' else tick.ask
                slot['hold_ticks'] += 1

                # Update peak
                if slot['side'] == 'BUY':
                    if cur > slot['peak_price']:
                        slot['peak_price'] = cur
                    pullback = (slot['peak_price'] - cur) / p
                else:
                    if cur < slot['peak_price']:
                        slot['peak_price'] = cur
                    pullback = (cur - slot['peak_price']) / p

                # Reversal exit after minimum hold
                if slot['hold_ticks'] >= MIN_HOLD_TICKS and pullback >= REVERSAL_PIPS:
                    _close_order(slot)
                    slot['active']      = False
                    slot['ticket']      = None
                    slot['close_after'] = now + REENTRY_MS / 1000.0

            # ── Open new positions on hot pairs ───────────────────────────
            for sym in _select_pairs():
                if sym not in _slots:
                    _slots[sym] = _new_slot(sym)
                slot = _slots[sym]

                if slot['active']:
                    continue
                if now < slot['close_after']:
                    continue

                bias = _get_bias(sym)
                if not bias:
                    continue

                ticket = _open_order(sym, bias)
                if ticket:
                    tick  = mt5.symbol_info_tick(sym)
                    entry = (tick.ask if bias == 'BUY' else tick.bid) if tick else 0.0
                    slot.update({
                        'active'     : True,
                        'side'       : bias,
                        'ticket'     : ticket,
                        'open_price' : entry,
                        'peak_price' : entry,
                        'hold_ticks' : 0,
                        'close_after': 0.0,
                    })

        time.sleep(POLL_MS / 1000.0)

    log.info('[PS] Main loop stopped')

# ─── Watchdog ─────────────────────────────────────────────────────────────────
def _watchdog():
    while _running:
        time.sleep(5)
        if not _running:
            break
        if _thread and not _thread.is_alive():
            log.error('[PS-WD] Thread died — restarting')
            _start_main_thread()

def _start_main_thread():
    global _thread
    _thread = threading.Thread(target=_loop, daemon=True, name='pulse_scalper')
    _thread.start()

# ─── Public API ───────────────────────────────────────────────────────────────
def start():
    global _running, _wd_thread
    _running = True
    _start_main_thread()
    _wd_thread = threading.Thread(target=_watchdog, daemon=True, name='pulse_scalper_wd')
    _wd_thread.start()
    log.info(
        f'[PS] 🚀 v1.0.0 started | pool={POOL} | '
        f'session={TRADING_START:02d}:00-{TRADING_END:02d}:00 UTC | '
        f'poll={POLL_MS}ms  entry_ticks={ENTRY_TICKS}  bias={ENTRY_BIAS:.0%}  '
        f'reversal={REVERSAL_PIPS}pip  reentry={REENTRY_MS}ms'
    )

def stop():
    global _running
    _running = False
    log.info('[PS] Stopped')

def get_state():
    with _slots_lock:
        return {
            'running': _running,
            'slots': {
                sym: {
                    'active'     : s['active'],
                    'side'       : s['side'],
                    'peak_price' : round(s['peak_price'], 5),
                    'hold_ticks' : s['hold_ticks'],
                }
                for sym, s in _slots.items()
            },
        }
