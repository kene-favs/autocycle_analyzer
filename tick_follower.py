"""
tick_follower.py  —  Ant-on-Sugar Tick Velocity Follower  v3.6.0
═══════════════════════════════════════════════════════════════════
7-pair dynamic pool — system picks the 2 hottest pairs automatically.
Zero spread pairs only.  Trail the peak.  Every real move = profit.

Pool: EURUSD · AUDUSD · GBPUSD · NZDUSD · USDJPY · USDCAD · USDCHF

Session : 05:00 – 20:00 UTC  (London + NY — maximum volatility window)
Selection: scores all 7 pairs every 5s by velocity, always runs top 2
Slots    : Slot A + Slot B — both dynamic, no fixed anchor
Lot size : scales with account balance (see _LOT_TIERS)

Entry logic
───────────
  2-tick confirmation before entry:
    t1→t2 interval primes direction (≥ 0.01 pip same way)
    t2→t3 interval confirms it     (≥ ENTRY_THRESHOLD_PIPS same way)
    Both must agree → enter.  Filters single-tick noise before any order is sent.

Exit logic
──────────
  TAKE PROFIT     : close immediately at TAKE_PROFIT_PIPS (0.80 pip default).
                    Net after $0.04 commission at 0.01 lot = +$0.04 per win.
                    Re-entry fires 20ms later — catches next move (continuation OR reversal).
                    Set TF_TP_PIPS=0 to disable and use pure trailing stop instead.

  BREAK-EVEN LOCK : once profit ≥ LOCK_TRIGGER_PIPS (0.50), floor rises to
                    LOCK_FLOOR_PIPS (0.42).  A winner can never fall back to a loser.
                    (Only active when TP is disabled.)

  TRAILING STOP   : fallback when TP is disabled or price overshoots.
                    when price pulls back TRAIL_PIPS (0.20) from peak → close.

  HARD STOP       : if profit drops to -MAX_LOSS_PIPS (-0.12 internal, ~-0.20 real)
                    → close immediately.

  Re-entry        : 20ms after any close → check direction → enter immediately if signal.
                    TP fired + price still rising → BUY again instantly.
                    TP fired + price reversing → SELL fires in the very next tick.

Safety : server-side SL = 1.2 pips  (crash guard only — watchdog fires first)
Poll   : every 20ms

Env overrides
─────────────
  TF_POLL_MS        20      tick poll interval (ms)
  TF_ENTRY_PIPS     0.01    min pressure to trigger entry (pips)
  TF_TRAIL_PIPS     0.20    pullback from peak to exit (pips)
  TF_MAX_LOSS       0.12    hard stop internal (pips) — fires at 0.12, lands ~0.20 real
  TF_TP_PIPS        0.80    fixed take-profit (pips) — close & re-enter (0 = trail only)
  TF_LOCK_TRIGGER   0.50    once up this much, lock in floor (trail-only mode)
  TF_LOCK_FLOOR     0.42    locked floor, above break-even (trail-only mode)
  TF_SAFETY_SL      1.2     emergency SL (pips) — crash guard only
  TF_REENTRY_MS     20      pause after close before re-entry (ms)
"""

import os
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Pool & session ────────────────────────────────────────────────────────────
POOL = ['EURUSD', 'AUDUSD', 'GBPUSD', 'NZDUSD', 'USDJPY', 'USDCAD', 'USDCHF']

TRADING_START        = 5    # 05:00 UTC = 06:00 WAT  — London + NY session
TRADING_END          = 20   # 20:00 UTC = 21:00 WAT  — end of NY session

# ── Timing ────────────────────────────────────────────────────────────────────
POLL_MS              = int(os.getenv('TF_POLL_MS',       '20'))
STALL_WINDOW_MS      = 30      # look back 30ms to detect stall — close fast when price stops
SCORE_WINDOW_MS      = 1000    # pair-scoring look-back window
RE_ENTRY_PAUSE_MS    = int(os.getenv('TF_REENTRY_MS',    '20'))   # 20ms — catch next tick immediately after real exit
WATCHDOG_INTERVAL_S  = 5

# ── Thresholds ────────────────────────────────────────────────────────────────
ENTRY_THRESHOLD_PIPS = float(os.getenv('TF_ENTRY_PIPS',   '0.01'))  # enter on any real tick — fast as possible
TRAIL_PIPS           = float(os.getenv('TF_TRAIL_PIPS',   '0.20'))  # pullback from peak → close
MAX_LOSS_PIPS        = float(os.getenv('TF_MAX_LOSS',     '0.12'))  # hard stop — fires at 0.12, lands ~0.20 after poll overshoot
TAKE_PROFIT_PIPS     = float(os.getenv('TF_TP_PIPS',      '0.80'))  # fixed take-profit — close & re-enter immediately (0 = disabled, use trail only)
LOCK_TRIGGER_PIPS    = float(os.getenv('TF_LOCK_TRIGGER', '0.50'))  # once up this much → lock break-even floor
LOCK_FLOOR_PIPS      = float(os.getenv('TF_LOCK_FLOOR',   '0.42'))  # locked floor (above 0.40 commission = guaranteed profit)
SAFETY_SL_PIPS       = float(os.getenv('TF_SAFETY_SL',    '1.2'))

# ── Order constants ───────────────────────────────────────────────────────────
MAGIC     = 20260814
DEVIATION = 20

# ── Lot tiers (by account balance) ───────────────────────────────────────────
_LOT_TIERS = [
    (30,    0.01),
    (50,    0.05),
    (100,   0.10),
    (200,   0.20),
    (400,   0.40),
    (800,   0.80),
    (2000,  1.00),
    (4000,  2.00),
    (8000,  4.00),
]

def _lot_for_balance(balance: float) -> float:
    """Return lot size for the given account balance."""
    for threshold, lot in _LOT_TIERS:
        if balance < threshold:
            return lot
    # Above $8 000: double every time balance doubles past $8 000
    lot = 4.00
    cap = 8000.0
    while balance >= cap * 2:
        lot *= 2.0
        cap *= 2.0
    return lot


# ── Pair scorer state (deque per symbol, last SCORE_WINDOW_MS of ticks) ───────
_scorer_ticks    = {sym: deque(maxlen=60) for sym in POOL}
_velocity_scores = {sym: 0.0              for sym in POOL}


# ── Slot factory ──────────────────────────────────────────────────────────────
def _new_slot(symbol: str) -> dict:
    return {
        'symbol'           : symbol,
        'active'           : False,
        'confirmed'        : False,   # True after tick 2 continues same direction (or BIG_TICK)
        'side'             : None,
        'ticket'           : None,
        'open_price'       : 0.0,
        'peak_price'       : 0.0,    # best price reached — trailing stop tracks from here
        'opened_at'        : 0.0,
        'closed_at'        : 0.0,
        'ticks'            : deque(maxlen=25),
        # stats (cumulative, survives symbol switches)
        'locked'           : False,  # True once profit hits LOCK_TRIGGER_PIPS — floor rises to LOCK_FLOOR_PIPS
        'trade_count'      : 0,
        'total_pips'       : 0.0,
        'last_pips'        : 0.0,
        'last_reason'      : '',
    }

_slots: dict[str, dict] = {
    'A': _new_slot(POOL[0]),
    'B': _new_slot(POOL[1]),
}

_stop_event              = threading.Event()
_loop_thread: threading.Thread | None = None

# ── Trade history (last 100 closed trades) ────────────────────────────────────
_HISTORY_MAX  = 100
_tf_history: list[dict] = []


# ── Session ───────────────────────────────────────────────────────────────────
def _in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    if TRADING_START > TRADING_END:          # overnight session (e.g. 22:00 → 20:00 next day)
        return h >= TRADING_START or h < TRADING_END
    return TRADING_START <= h < TRADING_END


# ── MT5 helpers ───────────────────────────────────────────────────────────────
def _pip(sym: str) -> float:
    """
    Pip size for any symbol:
      digits=5  (e.g. EURUSD)  → point × 10 = 0.00010
      digits=3  (e.g. USDJPY)  → point × 10 = 0.01000
      digits=4  (e.g. legacy)  → point       = 0.00010
    """
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
        return {
            'time': time.time(),
            'bid' : t.bid,
            'ask' : t.ask,
            'mid' : round((t.bid + t.ask) / 2, 6),
        }
    except Exception:
        return None

def _get_balance() -> float:
    try:
        import MetaTrader5 as mt5
        info = mt5.account_info()
        return float(info.balance) if info else 1000.0
    except Exception:
        return 1000.0

def _velocity(slot: dict, window_ms: int = STALL_WINDOW_MS) -> float:
    """Net pip change over last window_ms milliseconds.  + = rising, − = falling."""
    if len(slot['ticks']) < 2:
        return 0.0
    cutoff = time.time() - window_ms / 1000
    recent = [t for t in slot['ticks'] if t['time'] >= cutoff]
    if len(recent) < 2:
        recent = list(slot['ticks'])[-2:]
    pip = _pip(slot['symbol'])
    return (recent[-1]['mid'] - recent[0]['mid']) / pip if pip else 0.0


# ── Pair scorer ───────────────────────────────────────────────────────────────
def _score_all_pairs() -> None:
    """
    Poll every pool pair, calculate absolute pip velocity over SCORE_WINDOW_MS,
    store in _velocity_scores.  Called every 5 seconds by the main loop.
    """
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
    """Return the two most volatile pool pairs by current score."""
    ranked = sorted(POOL, key=lambda s: _velocity_scores.get(s, 0.0), reverse=True)
    return ranked[0], ranked[1]


# ── Order helpers ─────────────────────────────────────────────────────────────
def _open(slot: dict, side: str) -> bool:
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
        sl    = round(
            price - SAFETY_SL_PIPS * pip if side == 'BUY'
            else price + SAFETY_SL_PIPS * pip,
            5,
        )
        req = {
            'action'   : mt5.TRADE_ACTION_DEAL,
            'symbol'   : sym,
            'volume'   : lot,
            'type'     : mt5.ORDER_TYPE_BUY if side == 'BUY' else mt5.ORDER_TYPE_SELL,
            'price'    : price,
            'sl'       : sl,
            'deviation': DEVIATION,
            'magic'    : MAGIC,
            'comment'  : f'TF-{side}',
            'type_time': mt5.ORDER_TIME_GTC,
        }
        for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            req['type_filling'] = fill
            res = mt5.order_send(req)
            if res and res.retcode == 10030:
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                slot.update(
                    active=True, confirmed=False, side=side, ticket=res.order,
                    open_price=price, peak_price=price, opened_at=time.time(),
                )
                log.info(
                    f'[TF] ✅ OPEN  {sym} {side} @ {price:.5f}  '
                    f'SL={sl:.5f}  lot={lot}  bal=${bal:.0f}  ticket={res.order}'
                )
                return True
            if res:
                log.warning(f'[TF] open fail {sym} fill={fill} retcode={res.retcode}')
                return False
    except Exception as e:
        log.error(f'[TF] open exception {sym}: {e}')
    return False


def _close(slot: dict, reason: str) -> None:
    sym    = slot['symbol']
    ticket = slot['ticket']
    side   = slot['side']
    pip    = _pip(sym)
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            # Emergency SL already fired on MT5 side
            pips = (
                (slot['peak_price'] - slot['open_price']) / pip if side == 'BUY'
                else (slot['open_price'] - slot['peak_price']) / pip
            )
            _record(slot, round(pips, 2), f'{reason}/SL-hit')
            return
        vol      = positions[0].volume
        tick     = mt5.symbol_info_tick(sym)
        if tick is None:
            return
        close_px = round(tick.bid if side == 'BUY' else tick.ask, 5)
        for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            res = mt5.order_send({
                'action'      : mt5.TRADE_ACTION_DEAL,
                'symbol'      : sym,
                'volume'      : vol,
                'type'        : mt5.ORDER_TYPE_SELL if side == 'BUY' else mt5.ORDER_TYPE_BUY,
                'price'       : close_px,
                'position'    : ticket,
                'deviation'   : DEVIATION,
                'magic'       : MAGIC,
                'comment'     : 'TF-CLOSE',
                'type_filling': fill,
                'type_time'   : mt5.ORDER_TIME_GTC,
            })
            if res and res.retcode == 10030:
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                pips = (
                    (close_px - slot['open_price']) / pip if side == 'BUY'
                    else (slot['open_price'] - close_px) / pip
                )
                log.info(f'[TF] ✅ CLOSE {sym} @ {close_px:.5f} | {pips:+.2f} pip | {reason}')
                _record(slot, round(pips, 2), reason)
                return
            if res:
                log.warning(f'[TF] close fail {sym} fill={fill} retcode={res.retcode}')
    except Exception as e:
        log.error(f'[TF] close exception {sym}: {e}')


def _record(slot: dict, pips: float, reason: str) -> None:
    # Capture before reset
    sym  = slot['symbol']
    side = slot['side']

    slot['last_pips']   = pips
    slot['last_reason'] = reason
    slot['total_pips']  = round(slot['total_pips'] + pips, 2)
    slot['trade_count'] += 1
    slot['closed_at']   = time.time()
    slot['active']      = False
    slot['locked']      = False
    slot['ticket']      = None
    slot['side']        = None
    slot['open_price']  = 0.0
    slot['peak_price']  = 0.0

    # Append to session history (newest first when reversed in get_state)
    _tf_history.append({
        'ts'    : datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'symbol': sym,
        'side'  : side,
        'pips'  : round(pips, 2),
        'reason': reason,
    })
    if len(_tf_history) > _HISTORY_MAX:
        _tf_history.pop(0)


# ── Emergency close — watchdog safety ────────────────────────────────────────
def _emergency_close_all() -> None:
    """Close every open TF position on MT5.  Called when thread dies."""
    log.warning('[TF-WD] Running emergency close on all TF positions')
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(magic=MAGIC)
        if not positions:
            log.info('[TF-WD] No open TF positions found')
            return
        for pos in positions:
            sym      = pos.symbol
            side     = 'BUY' if pos.type == 0 else 'SELL'
            tick     = mt5.symbol_info_tick(sym)
            if tick is None:
                continue
            close_px = tick.bid if side == 'BUY' else tick.ask
            for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                res = mt5.order_send({
                    'action'      : mt5.TRADE_ACTION_DEAL,
                    'symbol'      : sym,
                    'volume'      : pos.volume,
                    'type'        : mt5.ORDER_TYPE_SELL if side == 'BUY' else mt5.ORDER_TYPE_BUY,
                    'price'       : close_px,
                    'position'    : pos.ticket,
                    'deviation'   : 20,
                    'magic'       : MAGIC,
                    'comment'     : 'TF-WATCHDOG',
                    'type_filling': fill,
                    'type_time'   : mt5.ORDER_TIME_GTC,
                })
                if res and res.retcode == 10030:
                    continue
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f'[TF-WD] ✅ Closed {sym} {side} ticket={pos.ticket}')
                    break
    except Exception as e:
        log.error(f'[TF-WD] Emergency close error: {e}')


# ── Per-slot 20ms handler ─────────────────────────────────────────────────────
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

    # ── IDLE: look for entry signal ───────────────────────────────────────────
    if not slot['active']:
        if not _in_session():
            return
        if len(slot['ticks']) < 2:
            return

        # 20ms re-entry pause — catch the very next tick after any close
        if slot['closed_at'] and time.time() - slot['closed_at'] < RE_ENTRY_PAUSE_MS / 1000:
            return

        if len(slot['ticks']) < 3:
            return

        t1 = slot['ticks'][-3]   # 40ms ago
        t2 = slot['ticks'][-2]   # 20ms ago
        t3 = slot['ticks'][-1]   # now

        # Two consecutive ticks in same direction = real momentum, not noise
        # t1→t2 primes the direction, t2→t3 confirms it — total delay: 40ms
        ask1 = (t2['ask'] - t1['ask']) / pip   # first interval
        ask2 = (t3['ask'] - t2['ask']) / pip   # second interval
        bid1 = (t2['bid'] - t1['bid']) / pip
        bid2 = (t3['bid'] - t2['bid']) / pip

        if ask1 >= 0.01 and ask2 >= ENTRY_THRESHOLD_PIPS:
            # Two consecutive ask rises — BUY confirmed, enter now
            if _open(slot, 'BUY'):
                slot['confirmed'] = True
        elif bid1 <= -0.01 and bid2 <= -ENTRY_THRESHOLD_PIPS:
            # Two consecutive bid falls — SELL confirmed, enter now
            if _open(slot, 'SELL'):
                slot['confirmed'] = True

    # ── ACTIVE: trail the peak, exit on reversal or hard stop ─────────────────
    else:
        # Skip the tick we entered on
        if time.time() - slot['opened_at'] < 0.04:
            return

        if slot['side'] == 'BUY':
            profit_pips = (mid - slot['open_price']) / pip
            # Update peak — highest mid seen since entry
            if mid > slot['peak_price']:
                slot['peak_price'] = mid
            pullback_pips = (slot['peak_price'] - mid) / pip
        else:
            profit_pips = (slot['open_price'] - mid) / pip
            # Update peak — lowest mid seen since entry
            if mid < slot['peak_price']:
                slot['peak_price'] = mid
            pullback_pips = (mid - slot['peak_price']) / pip

        # 1. Hard stop — price went MAX_LOSS_PIPS against entry
        if profit_pips <= -MAX_LOSS_PIPS:
            _close(slot, 'cut-loss')
            return

        # 2. Fixed take-profit — close immediately at target, re-enter on next tick
        #    Default 0.80 pip: net +$0.04 after $0.04 commission at 0.01 lot
        #    Re-entry fires in 20ms — catches the next move (continuation or reversal)
        if TAKE_PROFIT_PIPS > 0 and profit_pips >= TAKE_PROFIT_PIPS:
            _close(slot, 'tp')
            return

        # 3. Break-even lock — once up LOCK_TRIGGER_PIPS, raise floor to LOCK_FLOOR_PIPS
        #    Only active when TAKE_PROFIT_PIPS=0 (trailing-only mode)
        #    Ensures a trade that reaches 0.50+ pip can never fall back below commission
        if profit_pips >= LOCK_TRIGGER_PIPS:
            slot['locked'] = True
        if slot['locked'] and profit_pips <= LOCK_FLOOR_PIPS:
            _close(slot, 'lock-exit')
            return

        # 4. Trailing stop — fires when TP is disabled, or when price blows past TP
        #    (in practice TP fires first at 0.80; trail is the safety net for gaps)
        if pullback_pips >= TRAIL_PIPS:
            _close(slot, 'trail')


# ── Dynamic slot manager ──────────────────────────────────────────────────────
def _manage_slots() -> None:
    """
    Called every 5s.  Scores all pairs, finds best 2, switches any idle slot
    to a better pair.  Never interrupts an active trade.
    """
    b1, b2 = _best_two()
    desired = [b1, b2]

    # Symbols locked in active slots — don't touch those
    active_syms: set[str] = {s['symbol'] for s in _slots.values() if s['active']}
    used = set(active_syms)

    for key in ('A', 'B'):
        slot = _slots[key]
        if slot['active']:
            continue
        # Pick the best desired symbol not already claimed by another slot
        for sym in desired:
            if sym not in used:
                if slot['symbol'] != sym:
                    old = slot['symbol'] or '—'
                    log.info(
                        f'[TF] Slot-{key} {old} → {sym}  '
                        f'(score={_velocity_scores.get(sym, 0):.2f} pip/5s)'
                    )
                    slot['symbol'] = sym
                    slot['ticks'].clear()
                used.add(sym)
                break


# ── Main loop ─────────────────────────────────────────────────────────────────
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
        f'[TF] 🚀 v3.7.0 started | pool={POOL} | '
        f'session={TRADING_START:02d}:00-{TRADING_END:02d}:00 UTC | '
        f'poll={POLL_MS}ms entry={ENTRY_THRESHOLD_PIPS}pip '
        f'tp={TAKE_PROFIT_PIPS if TAKE_PROFIT_PIPS else "OFF"}pip '
        f'trail={TRAIL_PIPS}pip stop={MAX_LOSS_PIPS}pip SL={SAFETY_SL_PIPS}pip'
    )

    last_manage = 0.0
    while not _stop_event.is_set():
        t0 = time.perf_counter()
        try:
            now = time.time()
            # Score & slot-manage every 5 seconds
            if now - last_manage >= WATCHDOG_INTERVAL_S:
                _score_all_pairs()
                _manage_slots()
                last_manage = now
            # Tick both slots every 20ms
            _tick(_slots['A'])
            _tick(_slots['B'])
        except Exception as e:
            log.error(f'[TF] loop error: {e}', exc_info=True)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        time.sleep(max(0.0, POLL_MS - elapsed_ms) / 1000)


# ── Watchdog ──────────────────────────────────────────────────────────────────
def _watchdog() -> None:
    global _loop_thread
    log.info('[TF-WD] Watchdog started — checking thread every 5s')
    while not _stop_event.is_set():
        time.sleep(WATCHDOG_INTERVAL_S)
        if _loop_thread and not _loop_thread.is_alive() and not _stop_event.is_set():
            log.error('[TF-WD] ⚠️  Loop thread dead — closing positions and restarting')
            _emergency_close_all()
            t = threading.Thread(target=_loop, name='tick-follower', daemon=True)
            t.start()
            _loop_thread = t
            log.info('[TF-WD] ✅ Loop thread restarted')


# ── Public API ────────────────────────────────────────────────────────────────
def start() -> None:
    global _loop_thread
    _loop_thread = threading.Thread(target=_loop, name='tick-follower', daemon=True)
    _loop_thread.start()
    wd = threading.Thread(target=_watchdog, name='tick-follower-watchdog', daemon=True)
    wd.start()
    log.info('[TF] Main thread + watchdog thread started')

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
            'peak_price'  : s['peak_price'],
        }
    bal = _get_balance()
    return {
        'slot_a' : _snap(_slots['A']),
        'slot_b' : _snap(_slots['B']),
        'session': 'ACTIVE' if _in_session() else 'CLOSED',
        'scores' : {sym: _velocity_scores.get(sym, 0.0) for sym in POOL},
        'history': list(reversed(_tf_history)),   # newest first
        'config' : {
            'poll_ms'          : POLL_MS,
            'entry_pips'       : ENTRY_THRESHOLD_PIPS,
            'max_loss_pips'    : MAX_LOSS_PIPS,
            'trail_pips'       : TRAIL_PIPS,
            'take_profit_pips'  : TAKE_PROFIT_PIPS,
            'lock_trigger_pips' : LOCK_TRIGGER_PIPS,
            'lock_floor_pips'   : LOCK_FLOOR_PIPS,
            'safety_sl_pips'   : SAFETY_SL_PIPS,
            'lot'              : _lot_for_balance(bal),
            'balance'          : bal,
            'pool'             : POOL,
            'session_utc'      : f'{TRADING_START:02d}:00 – {TRADING_END:02d}:00',
        },
    }
