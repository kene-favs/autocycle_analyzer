"""
momentum_scalper.py  —  Directional Pressure Scalper  v1.0.0
══════════════════════════════════════════════════════════════
Runs ALONGSIDE range_break.  Different logic, different magic number.
They never interfere with each other.

THE IDEA
────────
Every 20ms, look at the last 50 ticks (~1 second of price data).
Count how many moved UP vs DOWN.

If 80%+ moved UP   → institutions are buying  → BUY immediately.
If 80%+ moved DOWN → institutions are selling → SELL immediately.

That level of directional consistency is NOT random noise.
It means a large order is being executed in one direction.
We front-run the rest of the move.

WHY IT'S DIFFERENT FROM TICK FOLLOWER
──────────────────────────────────────
Tick follower looks at 2-3 individual ticks.
This looks at 50 ticks — statistical weight, not a single event.
A random spike won't fool it. Only sustained directional pressure fires it.

MATH  (0.01 lot, EURUSD, $0.08 round-trip commission)
──────────────────────────────────────────────────────
  Win  = 5.0 pip × $0.10 − $0.08 = +$0.42 per win
  Loss = 2.0 pip × $0.10 + $0.08 = −$0.28 per loss
  R:R  = 1.5 : 1
  Win rate needed: 40%
  At 60% win rate: +$0.14 per trade → 15 trades/session = $2.10/day

SESSION : 07:00 – 21:00 UTC  (London + full NY — active institutional hours)
POOL    : EURUSD · AUDUSD · GBPUSD · NZDUSD · USDJPY · USDCAD · USDCHF
SLOTS   : 2 hottest pairs simultaneously

Env overrides
─────────────
  DP_WINDOW        50      ticks to look back (50 × 20ms = 1 second)
  DP_BIAS          0.80    fraction of ticks in same direction to trigger
  DP_TP_PIPS       5.0     take profit
  DP_SL_PIPS       2.0     stop loss
  DP_SAFETY_SL     5.0     server-side emergency SL
  DP_POLL_MS       20      poll interval
  DP_REENTRY_MS    2000    ms after close before re-entry (2 seconds)
  DP_SESSION_START 7       UTC hour session opens
  DP_SESSION_END   21      UTC hour session closes
"""

import os
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

POOL = ['EURUSD', 'AUDUSD', 'GBPUSD', 'NZDUSD', 'USDJPY', 'USDCAD', 'USDCHF']

TRADING_START    = int(os.getenv('DP_SESSION_START', '7'))
TRADING_END      = int(os.getenv('DP_SESSION_END',  '21'))

POLL_MS          = int(os.getenv('DP_POLL_MS',    '20'))
REENTRY_PAUSE_MS = int(os.getenv('DP_REENTRY_MS', '2000'))
WINDOW_TICKS     = int(os.getenv('DP_WINDOW',     '50'))
BIAS_THRESHOLD   = float(os.getenv('DP_BIAS',     '0.80'))
TP_PIPS          = float(os.getenv('DP_TP_PIPS',  '5.0'))
SL_PIPS          = float(os.getenv('DP_SL_PIPS',  '2.0'))
SAFETY_SL        = float(os.getenv('DP_SAFETY_SL','5.0'))
SCORE_WINDOW_MS  = 1000
WATCHDOG_S       = 5

MAGIC     = 20260817   # different from range_break (20260816) — no interference
DEVIATION = 20

_LOT_TIERS = [
    (500,   0.01),
    (1000,  0.02),
    (2000,  0.05),
    (5000,  0.10),
    (10000, 0.20),
    (20000, 0.50),
    (50000, 1.00),
]

def _lot_for_balance(balance: float) -> float:
    for threshold, lot in _LOT_TIERS:
        if balance < threshold:
            return lot
    lot = 4.00; cap = 8000.0
    while balance >= cap * 2:
        lot *= 2.0; cap *= 2.0
    return lot


_scorer_ticks    = {sym: deque(maxlen=200) for sym in POOL}
_velocity_scores = {sym: 0.0              for sym in POOL}


def _new_slot(symbol: str) -> dict:
    return {
        'symbol'      : symbol,
        'active'      : False,
        'side'        : None,
        'ticket'      : None,
        'open_price'  : 0.0,
        'opened_at'   : 0.0,
        'closed_at'   : 0.0,
        'ticks'       : deque(maxlen=max(WINDOW_TICKS + 10, 100)),
        'trade_count' : 0,
        'total_pips'  : 0.0,
        'last_pips'   : 0.0,
        'last_reason' : '',
        'last_bias'   : 0.0,
    }

_slots: dict[str, dict] = {
    'A': _new_slot(POOL[0]),
    'B': _new_slot(POOL[1]),
}

_stop_event              = threading.Event()
_loop_thread: threading.Thread | None = None
_HISTORY_MAX  = 100
_dp_history: list[dict] = []


def _in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    if TRADING_START > TRADING_END:
        return h >= TRADING_START or h < TRADING_END
    return TRADING_START <= h < TRADING_END


def _pip(sym: str) -> float:
    try:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(sym)
        if info is None:
            return 0.0001
        return info.point * 10 if info.digits in (5, 3) else info.point
    except Exception:
        return 0.0001

def _get_tick(sym: str) -> dict | None:
    try:
        import MetaTrader5 as mt5
        t = mt5.symbol_info_tick(sym)
        if t is None or t.bid <= 0:
            return None
        return {'time': time.time(), 'bid': t.bid, 'ask': t.ask,
                'mid': round((t.bid + t.ask) / 2, 6)}
    except Exception:
        return None

def _get_balance() -> float:
    try:
        import MetaTrader5 as mt5
        info = mt5.account_info()
        return float(info.balance) if info else 1000.0
    except Exception:
        return 1000.0


def _score_all_pairs() -> None:
    cutoff = time.time() - SCORE_WINDOW_MS / 1000
    for sym in POOL:
        tick = _get_tick(sym)
        if tick:
            _scorer_ticks[sym].append(tick)
        recent = [t for t in _scorer_ticks[sym] if t['time'] >= cutoff]
        if len(recent) < 2:
            _velocity_scores[sym] = 0.0
            continue
        pip = _pip(sym)
        vel = abs((recent[-1]['mid'] - recent[0]['mid']) / pip) if pip else 0.0
        _velocity_scores[sym] = round(vel, 3)

def _best_two() -> tuple[str, str]:
    ranked = sorted(POOL, key=lambda s: _velocity_scores.get(s, 0.0), reverse=True)
    return ranked[0], ranked[1]


def _get_bias(slot: dict) -> tuple[float, str]:
    """
    Returns (bias_fraction, direction) from last WINDOW_TICKS ticks.
    bias_fraction = fraction of tick-pairs moving in dominant direction.
    direction = 'BUY' | 'SELL' | ''
    """
    ticks = list(slot['ticks'])
    if len(ticks) < WINDOW_TICKS:
        return 0.0, ''
    recent = ticks[-WINDOW_TICKS:]
    rising  = sum(1 for i in range(1, len(recent)) if recent[i]['mid'] > recent[i-1]['mid'])
    falling = sum(1 for i in range(1, len(recent)) if recent[i]['mid'] < recent[i-1]['mid'])
    total   = rising + falling
    if total == 0:
        return 0.0, ''
    if rising > falling:
        return rising / total, 'BUY'
    else:
        return falling / total, 'SELL'


def _open(slot: dict, side: str, bias: float) -> bool:
    sym = slot['symbol']
    pip = _pip(sym)
    bal = _get_balance()
    lot = _lot_for_balance(bal)
    try:
        import MetaTrader5 as mt5
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return False
        price = round(tick.ask if side == 'BUY' else tick.bid, 5)
        sl    = round(price - SAFETY_SL * pip if side == 'BUY' else price + SAFETY_SL * pip, 5)
        req   = {
            'action': mt5.TRADE_ACTION_DEAL, 'symbol': sym, 'volume': lot,
            'type': mt5.ORDER_TYPE_BUY if side == 'BUY' else mt5.ORDER_TYPE_SELL,
            'price': price, 'sl': sl, 'deviation': DEVIATION,
            'magic': MAGIC, 'comment': f'DP-{side}', 'type_time': mt5.ORDER_TIME_GTC,
        }
        for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            req['type_filling'] = fill
            res = mt5.order_send(req)
            if res and res.retcode == 10030:
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                slot.update(active=True, side=side, ticket=res.order,
                            open_price=price, opened_at=time.time(), last_bias=bias)
                log.info(
                    f'[DP] ✅ OPEN  {sym} {side} @ {price:.5f}  '
                    f'bias={bias:.0%}  lot={lot}  bal=${bal:.2f}'
                )
                return True
            if res:
                log.warning(f'[DP] open fail {sym} retcode={res.retcode}')
                return False
    except Exception as e:
        log.error(f'[DP] open exception {sym}: {e}')
    return False


def _close(slot: dict, reason: str) -> None:
    sym = slot['symbol']; ticket = slot['ticket']; side = slot['side']
    pip = _pip(sym)
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            _record(slot, -SL_PIPS, f'{reason}/SL-hit')
            return
        vol      = positions[0].volume
        tick     = mt5.symbol_info_tick(sym)
        if tick is None:
            return
        close_px = round(tick.bid if side == 'BUY' else tick.ask, 5)
        for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            res = mt5.order_send({
                'action': mt5.TRADE_ACTION_DEAL, 'symbol': sym, 'volume': vol,
                'type': mt5.ORDER_TYPE_SELL if side == 'BUY' else mt5.ORDER_TYPE_BUY,
                'price': close_px, 'position': ticket, 'deviation': DEVIATION,
                'magic': MAGIC, 'comment': 'DP-CLOSE',
                'type_filling': fill, 'type_time': mt5.ORDER_TIME_GTC,
            })
            if res and res.retcode == 10030:
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                pips = ((close_px - slot['open_price']) / pip if side == 'BUY'
                        else (slot['open_price'] - close_px) / pip)
                log.info(f'[DP] ✅ CLOSE {sym} @ {close_px:.5f} | {pips:+.2f} pip | {reason}')
                _record(slot, round(pips, 2), reason)
                return
            if res:
                log.warning(f'[DP] close fail {sym} retcode={res.retcode}')
    except Exception as e:
        log.error(f'[DP] close exception {sym}: {e}')


def _record(slot: dict, pips: float, reason: str) -> None:
    sym = slot['symbol']; side = slot['side']
    slot['last_pips']   = pips
    slot['last_reason'] = reason
    slot['total_pips']  = round(slot['total_pips'] + pips, 2)
    slot['trade_count'] += 1
    slot['closed_at']   = time.time()
    slot['active']      = False
    slot['ticket']      = None
    slot['side']        = None
    slot['open_price']  = 0.0
    _dp_history.append({
        'ts': datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'symbol': sym, 'side': side,
        'pips': round(pips, 2), 'reason': reason,
    })
    if len(_dp_history) > _HISTORY_MAX:
        _dp_history.pop(0)


def _emergency_close_all() -> None:
    log.warning('[DP-WD] Emergency close all DP positions')
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(magic=MAGIC)
        if not positions:
            return
        for pos in positions:
            sym  = pos.symbol
            side = 'BUY' if pos.type == 0 else 'SELL'
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                continue
            close_px = tick.bid if side == 'BUY' else tick.ask
            for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                res = mt5.order_send({
                    'action': mt5.TRADE_ACTION_DEAL, 'symbol': sym, 'volume': pos.volume,
                    'type': mt5.ORDER_TYPE_SELL if side == 'BUY' else mt5.ORDER_TYPE_BUY,
                    'price': close_px, 'position': pos.ticket, 'deviation': 20,
                    'magic': MAGIC, 'comment': 'DP-WATCHDOG',
                    'type_filling': fill, 'type_time': mt5.ORDER_TIME_GTC,
                })
                if res and res.retcode == 10030:
                    continue
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f'[DP-WD] ✅ Closed {sym} {side}')
                    break
    except Exception as e:
        log.error(f'[DP-WD] Emergency close error: {e}')


def _tick(slot: dict) -> None:
    if not slot['symbol']:
        return
    tick = _get_tick(slot['symbol'])
    if tick is None:
        return
    slot['ticks'].append(tick)
    mid = tick['mid']
    pip = _pip(slot['symbol'])
    if not pip:
        return
    now = time.time()

    if not slot['active']:
        if not _in_session():
            return
        if slot['closed_at'] and now - slot['closed_at'] < REENTRY_PAUSE_MS / 1000:
            return
        if len(slot['ticks']) < WINDOW_TICKS:
            return

        bias, direction = _get_bias(slot)

        if bias >= BIAS_THRESHOLD and direction:
            log.info(
                f'[DP] {slot["symbol"]} {direction} pressure {bias:.0%} '
                f'({WINDOW_TICKS} ticks)'
            )
            _open(slot, direction, bias)

    else:
        if now - slot['opened_at'] < 0.04:
            return
        pip_val = _pip(slot['symbol'])
        profit_pips = ((mid - slot['open_price']) / pip_val if slot['side'] == 'BUY'
                       else (slot['open_price'] - mid) / pip_val)
        if profit_pips >= TP_PIPS:
            _close(slot, 'tp')
            return
        if profit_pips <= -SL_PIPS:
            _close(slot, 'cut-loss')


def _manage_slots() -> None:
    b1, b2 = _best_two()
    desired = [b1, b2]
    active_syms: set[str] = {s['symbol'] for s in _slots.values() if s['active']}
    used = set(active_syms)
    for key in ('A', 'B'):
        slot = _slots[key]
        if slot['active']:
            continue
        for sym in desired:
            if sym not in used:
                if slot['symbol'] != sym:
                    log.info(
                        f'[DP] Slot-{key} {slot["symbol"] or "—"} → {sym}  '
                        f'(score={_velocity_scores.get(sym, 0):.2f} pip/5s)'
                    )
                    slot['symbol'] = sym
                    slot['ticks'].clear()
                used.add(sym)
                break


def _loop() -> None:
    try:
        import MetaTrader5 as mt5
        for sym in POOL:
            try:
                mt5.symbol_select(sym, True)
            except Exception:
                pass
    except Exception:
        pass

    log.info(
        f'[DP] 🚀 v1.0.0 started | pool={POOL} | '
        f'session={TRADING_START:02d}:00-{TRADING_END:02d}:00 UTC | '
        f'poll={POLL_MS}ms  window={WINDOW_TICKS}ticks  '
        f'bias={BIAS_THRESHOLD:.0%}  tp={TP_PIPS}pip  sl={SL_PIPS}pip'
    )

    last_manage = 0.0
    while not _stop_event.is_set():
        t0 = time.perf_counter()
        try:
            now = time.time()
            if now - last_manage >= WATCHDOG_S:
                _score_all_pairs()
                _manage_slots()
                last_manage = now
            _tick(_slots['A'])
            _tick(_slots['B'])
        except Exception as e:
            log.error(f'[DP] loop error: {e}', exc_info=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        time.sleep(max(0.0, POLL_MS - elapsed_ms) / 1000)


def _watchdog() -> None:
    global _loop_thread
    log.info('[DP-WD] Watchdog started')
    while not _stop_event.is_set():
        time.sleep(WATCHDOG_S)
        if _loop_thread and not _loop_thread.is_alive() and not _stop_event.is_set():
            log.error('[DP-WD] ⚠️ Thread dead — restarting')
            _emergency_close_all()
            t = threading.Thread(target=_loop, name='momentum-scalper', daemon=True)
            t.start()
            _loop_thread = t


def start() -> None:
    global _loop_thread
    _loop_thread = threading.Thread(target=_loop, name='momentum-scalper', daemon=True)
    _loop_thread.start()
    wd = threading.Thread(target=_watchdog, name='momentum-scalper-watchdog', daemon=True)
    wd.start()
    log.info('[DP] Main thread + watchdog started')

def stop() -> None:
    _stop_event.set()

def get_state() -> dict:
    def _snap(s: dict) -> dict:
        return {
            'symbol'      : s['symbol'],
            'active'      : s['active'],
            'side'        : s['side'],
            'trade_count' : s['trade_count'],
            'total_pips'  : s['total_pips'],
            'last_pips'   : s['last_pips'],
            'last_reason' : s['last_reason'],
            'last_bias'   : s['last_bias'],
        }
    bal = _get_balance()
    return {
        'slot_a' : _snap(_slots['A']),
        'slot_b' : _snap(_slots['B']),
        'session': 'ACTIVE' if _in_session() else 'CLOSED',
        'scores' : {sym: _velocity_scores.get(sym, 0.0) for sym in POOL},
        'history': list(reversed(_dp_history)),
        'config' : {
            'poll_ms'      : POLL_MS,
            'window_ticks' : WINDOW_TICKS,
            'bias_threshold': BIAS_THRESHOLD,
            'tp_pips'      : TP_PIPS,
            'sl_pips'      : SL_PIPS,
            'lot'          : _lot_for_balance(bal),
            'balance'      : bal,
            'pool'         : POOL,
            'session_utc'  : f'{TRADING_START:02d}:00 – {TRADING_END:02d}:00',
        },
    }
