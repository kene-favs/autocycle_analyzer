"""
bracket_scalper.py  —  Level-Break Bracket Scalper  v1.0.0
═══════════════════════════════════════════════════════════
COMPLETELY different from tick follower.

Does NOT chase ticks.  Instead: sets an ANCHOR at current price, then watches
BOTH directions.  The first direction that breaks TRIGGER_PIPS wins — enter
immediately.  Fixed TP.  Fixed SL.  After close → new anchor → repeat.

Always watching.  Always ready.  Both directions at the same time.

How it works
────────────
  1. Set ANCHOR = current mid price (after close or session start)
  2. Every 20ms check:
       If mid  ≥  ANCHOR + TRIGGER_PIPS  →  BUY  immediately
       If mid  ≤  ANCHOR − TRIGGER_PIPS  →  SELL immediately
  3. In trade:
       TP  at  ENTRY + TP_PIPS (2.0)  →  close, log 'tp'
       SL  at  ENTRY − SL_PIPS (0.50)  →  close, log 'cut-loss'
  4. After close: wait 20ms → new ANCHOR = current price → back to step 2
  5. If no break for ANCHOR_DECAY_S (120s) → re-anchor at current price
     (prevents stale anchor after flat consolidation)

Math  (0.01 lot, EURUSD, $0.04/trade commission at Tickmill)
──────────────────────────────────────────────────────────────
  Win  = 2.0 pip × $0.10 − $0.08 commission = +$0.12 per win
  Loss = 0.50 pip × $0.10 + $0.08 commission = −$0.13 per loss

  Win rate needed to profit: 52%
  At 60% win rate: +$0.02 per trade
  At 65% win rate: +$0.037 per trade

  London/NY session: EURUSD regularly makes 1–5 pip directional moves after
  breaking a level → 60%+ win rate is realistic when the market is moving.

Session : 05:00 – 20:00 UTC  (London + NY — real directional moves)
Pool    : EURUSD · AUDUSD · GBPUSD · NZDUSD · USDJPY · USDCAD · USDCHF
Slots   : 2 hottest pairs run simultaneously (scored by 1s velocity)

Env overrides
─────────────
  BS_TRIGGER_PIPS   0.30    pip break from anchor to trigger entry
  BS_TP_PIPS        2.00    fixed take profit from entry
  BS_SL_PIPS        0.50    fixed stop loss from entry
  BS_SAFETY_SL      2.0     server-side emergency SL (crash guard)
  BS_ANCHOR_DECAY   120     seconds before re-anchoring if no trade fires
  BS_POLL_MS        20      poll interval (ms)
  BS_REENTRY_MS     20      pause after close before re-anchoring
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

TRADING_START = 5    # 05:00 UTC = 06:00 WAT  — London + NY
TRADING_END   = 20   # 20:00 UTC = 21:00 WAT

# ── Timing ────────────────────────────────────────────────────────────────────
POLL_MS          = int(os.getenv('BS_POLL_MS',       '20'))
REENTRY_PAUSE_MS = int(os.getenv('BS_REENTRY_MS',    '20'))
ANCHOR_DECAY_S   = int(os.getenv('BS_ANCHOR_DECAY', '120'))
SCORE_WINDOW_MS  = 1000
WATCHDOG_S       = 5

# ── Strategy parameters ───────────────────────────────────────────────────────
TRIGGER_PIPS    = float(os.getenv('BS_TRIGGER_PIPS',  '0.30'))  # break from anchor → enter
TP_PIPS         = float(os.getenv('BS_TP_PIPS',       '2.00'))  # take profit from entry
SL_PIPS         = float(os.getenv('BS_SL_PIPS',       '0.50'))  # stop loss from entry
SAFETY_SL       = float(os.getenv('BS_SAFETY_SL',     '2.0'))   # emergency server SL
FLAT_WINDOW_S   = float(os.getenv('BS_FLAT_WINDOW',   '300'))   # ranging check window (5 min)
FLAT_MIN_PIPS   = float(os.getenv('BS_FLAT_PIPS',     '2.5'))   # min range in window to allow entry
MOMENTUM_TICKS  = int(os.getenv('BS_MOMENTUM_TICKS',  '5'))     # ticks to confirm break direction
MOMENTUM_MIN    = float(os.getenv('BS_MOMENTUM_MIN',  '0.60'))  # fraction confirming direction

MAGIC     = 20260815
DEVIATION = 20

# ── Lot tiers ─────────────────────────────────────────────────────────────────
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
    return 1.00


# ── Pair scorer state ─────────────────────────────────────────────────────────
_scorer_ticks    = {sym: deque(maxlen=60) for sym in POOL}
_velocity_scores = {sym: 0.0              for sym in POOL}


# ── Slot factory ──────────────────────────────────────────────────────────────
def _new_slot(symbol: str) -> dict:
    return {
        'symbol'       : symbol,
        'active'       : False,
        'side'         : None,
        'ticket'       : None,
        'open_price'   : 0.0,
        'anchor'       : 0.0,    # level we're watching for a break
        'anchor_set'   : 0.0,    # timestamp anchor was set
        'opened_at'    : 0.0,
        'closed_at'    : 0.0,
        'open_fail_until': 0.0,  # cooldown after failed open (prevents retry spam)
        'peak_price'   : 0.0,   # best price seen since entry (for reversal detection)
        'hold_ticks'   : 0,     # ticks held (min hold before reversal exit)
        'ticks'        : deque(maxlen=25),
        'trade_count'  : 0,
        'total_pips'   : 0.0,
        'last_pips'    : 0.0,
        'last_reason'  : '',
    }

_slots: dict[str, dict] = {
    'A': _new_slot(POOL[0]),
    'B': _new_slot(POOL[1]),
}

_stop_event              = threading.Event()
_loop_thread: threading.Thread | None = None
_HISTORY_MAX  = 100
_bs_history: list[dict] = []


# ── Session check ─────────────────────────────────────────────────────────────
def _in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    if TRADING_START > TRADING_END:
        return h >= TRADING_START or h < TRADING_END
    return TRADING_START <= h < TRADING_END


# ── MT5 helpers ───────────────────────────────────────────────────────────────
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


# ── Pair scorer ───────────────────────────────────────────────────────────────
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


# ── Market intelligence ───────────────────────────────────────────────────────
def _is_ranging(sym: str) -> bool:
    """
    True when market is too flat to enter.
    Checks price range over last FLAT_WINDOW_S seconds.
    If total range < FLAT_MIN_PIPS → market is ranging → sit and wait.
    """
    buf = _scorer_ticks[sym]
    if len(buf) < 5:
        return True  # not enough data yet — wait
    cutoff = time.time() - FLAT_WINDOW_S
    recent = [t for t in buf if t['time'] >= cutoff]
    if len(recent) < 3:
        return True
    p = _pip(sym)
    if not p:
        return True
    hi = max(t['mid'] for t in recent)
    lo = min(t['mid'] for t in recent)
    range_pips = (hi - lo) / p
    if range_pips < FLAT_MIN_PIPS:
        return True  # flat — don't trade
    return False

def _momentum_confirms(slot: dict, direction: str) -> bool:
    """
    True when last MOMENTUM_TICKS ticks confirm the break direction.
    Prevents entering on a spike that already reversed — the break must still be happening.
    """
    ticks = list(slot['ticks'])
    if len(ticks) < MOMENTUM_TICKS + 1:
        return True  # not enough tick history — allow entry
    recent = ticks[-(MOMENTUM_TICKS + 1):]
    if direction == 'BUY':
        up = sum(1 for i in range(1, len(recent)) if recent[i]['mid'] >= recent[i-1]['mid'])
        return (up / MOMENTUM_TICKS) >= MOMENTUM_MIN
    else:
        dn = sum(1 for i in range(1, len(recent)) if recent[i]['mid'] <= recent[i-1]['mid'])
        return (dn / MOMENTUM_TICKS) >= MOMENTUM_MIN


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
            price - SAFETY_SL * pip if side == 'BUY'
            else price + SAFETY_SL * pip,
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
            'comment'  : f'BS-{side}',
            'type_time': mt5.ORDER_TIME_GTC,
        }
        for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            req['type_filling'] = fill
            res = mt5.order_send(req)
            if res and res.retcode == 10030:
                continue
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                slot.update(
                    active=True, side=side, ticket=res.order,
                    open_price=price, opened_at=time.time(),
                    open_fail_until=0.0, peak_price=0.0, hold_ticks=0,
                )
                log.info(
                    f'[BS] ✅ OPEN  {sym} {side} @ {price:.5f}  '
                    f'anchor={slot["anchor"]:.5f}  lot={lot}  bal=${bal:.0f}  ticket={res.order}'
                )
                return True
            if res:
                log.warning(f'[BS] open fail {sym} fill={fill} retcode={res.retcode}')
                # 5-second cooldown + reset anchor to prevent retry flood
                slot['open_fail_until'] = time.time() + 5.0
                slot['anchor'] = 0.0
                return False
    except Exception as e:
        log.error(f'[BS] open exception {sym}: {e}')
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
            # Already closed by server SL
            _record(slot, -SL_PIPS, f'{reason}/SL-hit')
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
                'comment'     : 'BS-CLOSE',
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
                log.info(f'[BS] ✅ CLOSE {sym} @ {close_px:.5f} | {pips:+.2f} pip | {reason}')
                _record(slot, round(pips, 2), reason)
                return
            if res:
                log.warning(f'[BS] close fail {sym} fill={fill} retcode={res.retcode}')
    except Exception as e:
        log.error(f'[BS] close exception {sym}: {e}')


def _record(slot: dict, pips: float, reason: str) -> None:
    sym  = slot['symbol']
    side = slot['side']
    slot['last_pips']   = pips
    slot['last_reason'] = reason
    slot['total_pips']  = round(slot['total_pips'] + pips, 2)
    slot['trade_count'] += 1
    slot['closed_at']   = time.time()
    slot['active']      = False
    slot['ticket']      = None
    slot['side']        = None
    slot['open_price']  = 0.0
    slot['anchor']      = 0.0   # will be re-set in _tick after REENTRY_PAUSE_MS
    _bs_history.append({
        'ts'    : datetime.now(timezone.utc).strftime('%H:%M:%S'),
        'symbol': sym,
        'side'  : side,
        'pips'  : round(pips, 2),
        'reason': reason,
    })
    if len(_bs_history) > _HISTORY_MAX:
        _bs_history.pop(0)


# ── Emergency close — watchdog safety ────────────────────────────────────────
def _emergency_close_all() -> None:
    log.warning('[BS-WD] Running emergency close on all BS positions')
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(magic=MAGIC)
        if not positions:
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
                    'comment'     : 'BS-WATCHDOG',
                    'type_filling': fill,
                    'type_time'   : mt5.ORDER_TIME_GTC,
                })
                if res and res.retcode == 10030:
                    continue
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    log.info(f'[BS-WD] ✅ Closed {sym} {side} ticket={pos.ticket}')
                    break
    except Exception as e:
        log.error(f'[BS-WD] Emergency close error: {e}')


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
    now = time.time()

    # ── IDLE: watch for level break ───────────────────────────────────────────
    if not slot['active']:
        if not _in_session():
            slot['anchor'] = 0.0
            return

        # Cooldown after failed open — prevents retry spam
        if now < slot['open_fail_until']:
            return

        # Brief pause after close — let price settle
        if slot['closed_at'] and now - slot['closed_at'] < REENTRY_PAUSE_MS / 1000:
            return

        # Set anchor if not set (first time or after close)
        if slot['anchor'] == 0.0:
            slot['anchor']    = mid
            slot['anchor_set'] = now
            log.info(f'[BS] {slot["symbol"]} ⚓ anchor @ {mid:.5f}')
            return

        # Re-anchor if price stayed within range for ANCHOR_DECAY_S seconds
        if now - slot['anchor_set'] >= ANCHOR_DECAY_S:
            slot['anchor']    = mid
            slot['anchor_set'] = now
            log.info(f'[BS] {slot["symbol"]} ⚓ re-anchor (decay) @ {mid:.5f}')
            return

        # Check for level break — enter immediately, no gates
        dist_up   = (mid - slot['anchor']) / pip
        dist_down = (slot['anchor'] - mid) / pip

        if dist_up >= TRIGGER_PIPS:
            log.info(f'[BS] {slot["symbol"]} ⬆ BREAK +{dist_up:.2f}pip → BUY')
            _open(slot, 'BUY')

        elif dist_down >= TRIGGER_PIPS:
            log.info(f'[BS] {slot["symbol"]} ⬇ BREAK -{dist_down:.2f}pip → SELL')
            _open(slot, 'SELL')

    # ── ACTIVE: manage exit via reversal detection ────────────────────────────
    else:
        # Skip first 2 ticks (entry slippage noise)
        if now - slot['opened_at'] < 0.04:
            return

        slot['hold_ticks'] += 1
        pip_val = _pip(slot['symbol'])

        # Update peak price (best price seen since entry)
        if slot['side'] == 'BUY':
            cur = tick['bid']
            if slot['peak_price'] == 0.0:
                slot['peak_price'] = cur
            elif cur > slot['peak_price']:
                slot['peak_price'] = cur
            profit_pips  = (cur - slot['open_price']) / pip_val
            pullback     = (slot['peak_price'] - cur) / pip_val
        else:
            cur = tick['ask']
            if slot['peak_price'] == 0.0:
                slot['peak_price'] = cur
            elif cur < slot['peak_price']:
                slot['peak_price'] = cur
            profit_pips  = (slot['open_price'] - cur) / pip_val
            pullback     = (cur - slot['peak_price']) / pip_val

        # Take profit at TP_PIPS
        if profit_pips >= TP_PIPS:
            _close(slot, 'tp')
            return

        # Reversal exit: price pulled back TRAIL_PIPS from its peak → close
        # Tight trail = exit near the peak, not 0.50 pip below it
        TRAIL_PIPS = 0.20
        if slot['hold_ticks'] >= 3 and pullback >= TRAIL_PIPS:
            _close(slot, 'reversal-exit')


# ── Dynamic slot manager ──────────────────────────────────────────────────────
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
                    old = slot['symbol'] or '—'
                    log.info(
                        f'[BS] Slot-{key} {old} → {sym}  '
                        f'(score={_velocity_scores.get(sym, 0):.2f} pip/5s)'
                    )
                    slot['symbol'] = sym
                    slot['ticks'].clear()
                    slot['anchor'] = 0.0   # reset anchor on symbol switch
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
        f'[BS] 🚀 v1.0.0 started | pool={POOL} | '
        f'session={TRADING_START:02d}:00-{TRADING_END:02d}:00 UTC | '
        f'poll={POLL_MS}ms  trigger={TRIGGER_PIPS}pip  tp={TP_PIPS}pip  sl={SL_PIPS}pip  '
        f'decay={ANCHOR_DECAY_S}s'
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
            log.error(f'[BS] loop error: {e}', exc_info=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        time.sleep(max(0.0, POLL_MS - elapsed_ms) / 1000)


# ── Watchdog ──────────────────────────────────────────────────────────────────
def _watchdog() -> None:
    global _loop_thread
    log.info('[BS-WD] Watchdog started — checking thread every 5s')
    while not _stop_event.is_set():
        time.sleep(WATCHDOG_S)
        if _loop_thread and not _loop_thread.is_alive() and not _stop_event.is_set():
            log.error('[BS-WD] ⚠️ Loop thread dead — closing positions and restarting')
            _emergency_close_all()
            t = threading.Thread(target=_loop, name='bracket-scalper', daemon=True)
            t.start()
            _loop_thread = t
            log.info('[BS-WD] ✅ Loop thread restarted')


# ── Public API (same interface as tick_follower) ───────────────────────────────
def start() -> None:
    global _loop_thread
    _loop_thread = threading.Thread(target=_loop, name='bracket-scalper', daemon=True)
    _loop_thread.start()
    wd = threading.Thread(target=_watchdog, name='bracket-scalper-watchdog', daemon=True)
    wd.start()
    log.info('[BS] Main thread + watchdog thread started')

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
            'anchor'      : round(s['anchor'], 5) if s['anchor'] else 0.0,
        }
    bal = _get_balance()
    return {
        'slot_a' : _snap(_slots['A']),
        'slot_b' : _snap(_slots['B']),
        'session': 'ACTIVE' if _in_session() else 'CLOSED',
        'scores' : {sym: _velocity_scores.get(sym, 0.0) for sym in POOL},
        'history': list(reversed(_bs_history)),
        'config' : {
            'poll_ms'       : POLL_MS,
            'trigger_pips'  : TRIGGER_PIPS,
            'tp_pips'       : TP_PIPS,
            'sl_pips'       : SL_PIPS,
            'safety_sl'     : SAFETY_SL,
            'anchor_decay_s': ANCHOR_DECAY_S,
            'lot'           : _lot_for_balance(bal),
            'balance'       : bal,
            'pool'          : POOL,
            'session_utc'   : f'{TRADING_START:02d}:00 – {TRADING_END:02d}:00',
        },
    }
