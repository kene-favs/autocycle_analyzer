"""
autocycle_broker/engine.py
──────────────────────────
Price monitoring thread + hedge execution logic.

Phase state machine:
  IDLE  ──► BOTH_OPEN  ──► SURVIVOR  ──► COOLDOWN  ──► IDLE
                 └─ timeout (flat market) ──► COOLDOWN

IDLE        : waiting for /open call from bot
BOTH_OPEN   : BUY + SELL live internally; monitor mid vs SL levels
SURVIVOR    : one SL fired; Vantage hedge open; watching for TP or guardian
COOLDOWN    : cycle done; waiting COOLDOWN_SECS before accepting new cycle
"""
import logging
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
import MetaTrader5 as mt5
import numpy as np
import pandas as pd

from . import book, config

log = logging.getLogger('AutocycleBroker.Engine')


def _tg_alert(msg: str):
    """Send urgent Telegram alert (e.g. MT5 algo trading disabled)."""
    token   = os.getenv('TELEGRAM_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHANNEL_ID', '')
    if not token or not chat_id:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json    = {'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'},
            timeout = 8,
        )
    except Exception:
        pass


# ─── Level Gravity direction cache ───────────────────────────────────────────
# Updated by a background thread every 15s — reading it is instant (no MT5 call).
_gravity_cache: dict = {
    'verdict'    : 'SKIP',    # 'FIRE' or 'SKIP'
    'direction'  : None,      # 'BUY' or 'SELL' (when FIRE)
    'score'      : 0,         # momentum votes 0-5
    'skip_reason': 'not yet computed',
    'updated_at' : 0.0,       # epoch seconds of last successful update
}

def _gravity_loop():
    """
    Background thread: runs Level Gravity analysis every 15 seconds and
    caches the result.  No lock needed — dict writes are atomic in CPython.
    The engine and /open handler read _gravity_cache without calling MT5.
    """
    try:
        from level_gravity import analyze_gravity_scalp
    except ImportError:
        log.warning('level_gravity not found — gravity filter disabled')
        return

    while True:
        try:
            result = analyze_gravity_scalp('M1')
            _gravity_cache['verdict']     = result.get('verdict', 'SKIP')
            _gravity_cache['direction']   = result.get('direction')
            _gravity_cache['score']       = result.get('momentum_score', 0)
            _gravity_cache['skip_reason'] = result.get('skip_reason', '')
            _gravity_cache['updated_at']  = time.time()
            log.debug(
                f'[Gravity] {_gravity_cache["verdict"]} '
                f'dir={_gravity_cache["direction"]} '
                f'score={_gravity_cache["score"]} '
                f'reason={_gravity_cache["skip_reason"]}'
            )
        except Exception as exc:
            log.debug(f'Gravity update error: {exc}')
        time.sleep(15)


# ─── Simulation state (used when config.SIMULATE=True) ──────────────────────
# Tracks a synthetic mid price that drifts to exercise the full state machine.
_sim: dict = {
    'mid'  : 0.0,    # current simulated price
    'step' : 0.0,    # price change per tick (negative = drifting DOWN)
}

# ─── Shared state (read by broker.py for /status endpoint) ──────────────────
_state: dict = {
    'phase'            : 'IDLE',
    'cycle_id'         : None,
    'mid'              : 0.0,
    'atr'              : 0.0,
    'sl_dist'          : 0.0,
    'tp_extra'         : 0.0,
    'sl_fired_side'    : None,    # 'BUY' or 'SELL'
    'sl_price'         : 0.0,     # mid at moment SL fired
    'tp_target'        : 0.0,     # mid target for TP hit
    'hedge_ticket'     : None,
    'cooldown_until'   : 0.0,
    'both_open_since'  : 0.0,
    'error'            : None,
    # Active symbol for this cycle (set on open, fixed until cycle closes)
    'active_symbol'    : config.SYMBOL,
    'active_contract'  : config.CONTRACT_SIZE,
    'active_rev_tol'   : config.REVERSAL_TOLERANCE,
    'active_guard_tol' : config.GUARDIAN_TOLERANCE,
    # internal carry-over between phases (prefixed _)
    '_survivor_id'     : None,
    '_survivor_side'   : None,
    '_survivor_lot'    : 0.0,
    '_sl_pnl'          : 0.0,
    # Early hedge: Tickmill position opened before internal SL fires
    '_early_hedge_ticket': None,   # ticket if hedge was opened early, else None
    '_early_hedge_side'  : None,   # 'BUY' or 'SELL'
    '_early_hedge_price' : 0.0,    # mid price when early hedge was opened
}
_lock = threading.Lock()


# ─── Active symbol helpers ────────────────────────────────────────────────────

def _sym() -> str:
    """Return the symbol for the current (or upcoming) cycle."""
    with _lock:
        return _state.get('active_symbol', config.SYMBOL)


def get_active_profile() -> dict:
    """
    Return the instrument profile for the current London time.

    Schedule (when ENABLE_SCHEDULE=true):
      Gold  : Mon–Fri  05:00–21:00  London → XAUUSD+
      BTC   : Mon–Fri  21:00–05:00  London + weekends → BTCUSD

    Single-symbol mode: always returns the global config defaults.
    """
    if not config.ENABLE_SCHEDULE:
        return {
            'symbol'       : config.SYMBOL,
            'contract_size': config.CONTRACT_SIZE,
            'atr_min'      : config.ATR_MIN,
            'sl_min'       : config.SL_MIN,
            'sl_max'       : config.SL_MAX,
            'tp_extra_min' : config.TP_EXTRA_MIN,
            'tp_extra_max' : config.TP_EXTRA_MAX,
            'rev_tol'      : config.REVERSAL_TOLERANCE,
            'guard_tol'    : config.GUARDIAN_TOLERANCE,
        }

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('Europe/London'))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=1)))

    weekday  = now.weekday()   # 0=Mon … 6=Sun
    hour     = now.hour

    # Gold hours: strictly weekday AND within 05:00–21:00
    is_gold = (weekday < 5
               and config.GOLD_START_HOUR <= hour < config.GOLD_END_HOUR)

    if is_gold:
        return {
            'symbol'       : config.GOLD_SYMBOL,
            'contract_size': config.GOLD_CONTRACT,
            'atr_min'      : config.GOLD_ATR_MIN,
            'sl_min'       : config.GOLD_SL_MIN,
            'sl_max'       : config.GOLD_SL_MAX,
            'tp_extra_min' : config.GOLD_TP_MIN,
            'tp_extra_max' : config.GOLD_TP_MAX,
            'rev_tol'      : config.REVERSAL_TOLERANCE,
            'guard_tol'    : config.GUARDIAN_TOLERANCE,
        }
    else:
        return {
            'symbol'       : config.BTC_SYMBOL,
            'contract_size': config.BTC_CONTRACT,
            'atr_min'      : config.BTC_ATR_MIN,
            'sl_min'       : config.BTC_SL_MIN,
            'sl_max'       : config.BTC_SL_MAX,
            'tp_extra_min' : config.BTC_TP_MIN,
            'tp_extra_max' : config.BTC_TP_MAX,
            'rev_tol'      : config.BTC_REVERSAL_TOLERANCE,
            'guard_tol'    : config.BTC_GUARDIAN_TOLERANCE,
        }


def get_state() -> dict:
    with _lock:
        return {k: v for k, v in _state.items() if not k.startswith('_')}


def _s(key: str, value):
    with _lock:
        _state[key] = value


def _sm(updates: dict):
    with _lock:
        _state.update(updates)


# ─── MT5 helpers ─────────────────────────────────────────────────────────────

def _mt5_account_info() -> dict:
    """Return the currently connected MT5 account — used by /status endpoint."""
    try:
        info = mt5.account_info()
        if info:
            return {
                'login'  : info.login,
                'name'   : info.name,
                'server' : info.server,
                'balance': round(info.balance, 2),
                'type'   : 'HEDGING' if info.margin_mode == 2 else 'NETTING',
            }
    except Exception:
        pass
    return {'login': None, 'name': None, 'server': None, 'balance': 0.0, 'type': None}


def _mt5_connect() -> bool:
    if config.SIMULATE:
        # Try MT5 but don't fail if it's unavailable (weekend / offline)
        try:
            if mt5.account_info() is None:
                kw = dict(login=config.MT5_LOGIN,
                          password=config.MT5_PASSWORD,
                          server=config.MT5_SERVER)
                if config.MT5_PATH:
                    kw['path'] = config.MT5_PATH
                mt5.initialize(**kw)
        except Exception:
            pass
        return True   # always OK in simulate mode

    # Attach to the already-running MT5 terminal.
    # Specifying path lets the library find it directly (no 60-second search).
    # No login/server params = attach only, never spawn a second process.
    _term_exe = r'C:\Program Files\MetaTrader 5\terminal64.exe'
    if not mt5.initialize(path=_term_exe, timeout=15000):
        log.warning('[MT5] Cannot reach MT5 — make sure it is open and logged into Tickmill')
        return False

    info = mt5.account_info()
    if info is not None and info.login == config.MT5_LOGIN:
        log.info(f'MT5 connected: #{info.login} | {info.server} | ${info.balance:.2f}')
        return True

    # Connected to wrong account
    got = info.login if info else 'none'
    log.warning(f'[MT5] Terminal is on account #{got} — switch MT5 to Tickmill #{config.MT5_LOGIN} and the broker will reconnect')
    mt5.shutdown()
    return False


def _ensure_symbol(sym: str = None) -> bool:
    if config.SIMULATE:
        return True
    s = sym or _sym()
    info = mt5.symbol_info(s)
    if info is None:
        return False
    mt5.symbol_select(s, True)   # always refresh stream, not just when not visible
    return True


def _get_mid() -> float | None:
    """
    Returns the current mid price.
    Live mode  : reads from MT5 tick (bid+ask)/2.
    Simulate   : returns synthetic price that drifts toward SL then TP.
                 If MT5 happens to have a tick (e.g. Friday close still cached),
                 that tick is used to seed the base price on first call.
    """
    sym = _sym()

    if not config.SIMULATE:
        # Refresh stream before reading — prevents stale prices on Windows VPS
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick is None or tick.bid <= 0:
            return None
        # Reject zombie price — MT5 terminal lost its live feed from the broker.
        # tick.time is seconds since epoch (UTC). If older than 60s during
        # trading hours it means the terminal stopped receiving ticks.
        age = time.time() - tick.time
        if age > 60:
            log.warning(
                f'[MT5] Stale tick: {sym} last updated {age:.0f}s ago '
                f'(bid={tick.bid}) — rejecting zombie price'
            )
            return None
        return round((tick.bid + tick.ask) / 2, 2)

    # Simulate mode — read tick to seed synthetic price
    tick = mt5.symbol_info_tick(sym)

    # ── Simulate mode ──────────────────────────────────────────────────────────
    # Seed from real tick once if available, else use SIM_BASE_PRICE
    if _sim['mid'] == 0.0:
        if tick is not None:
            _sim['mid'] = round((tick.bid + tick.ask) / 2, 2)
            log.info(f'[SIM] Seeding from MT5 tick: mid={_sim["mid"]:.2f}')
        else:
            _sim['mid'] = config.SIM_BASE_PRICE
            log.info(f'[SIM] No MT5 tick — using SIM_BASE_PRICE={_sim["mid"]:.2f}')

    with _lock:
        phase    = _state['phase']
        sl_dist  = _state['sl_dist']
        tp_extra = _state['tp_extra']

    if phase == 'BOTH_OPEN':
        # Drift DOWN toward BUY SL over ~60 engine ticks (60 × 200ms = 12 s)
        # Add tiny noise to look realistic
        _sim['step'] = -(sl_dist / 60.0)
        _sim['mid']  = round(_sim['mid'] + _sim['step'] + random.uniform(-0.01, 0.01), 2)

    elif phase == 'SURVIVOR':
        # Continue DOWN past SL level toward TP over ~15 ticks (~3 s)
        _sim['step'] = -((tp_extra + 0.10) / 15.0)
        _sim['mid']  = round(_sim['mid'] + _sim['step'] + random.uniform(-0.005, 0.005), 2)

    return _sim['mid']


def _get_atr(sym: str = None) -> float:
    sym = sym or _sym()

    if config.SIMULATE:
        # Try real ATR first; fall back to configured sim value
        try:
            rates = mt5.copy_rates_from_pos(
                sym, mt5.TIMEFRAME_M1, 1, config.ATR_PERIOD + 3
            )
            if rates is not None and len(rates) >= config.ATR_PERIOD + 1:
                df = pd.DataFrame(rates)
                h, l, c = df['high'].values, df['low'].values, df['close'].values
                tr = np.maximum(
                    h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
                )
                if len(tr) >= config.ATR_PERIOD:
                    atr = float(np.mean(tr[-config.ATR_PERIOD:]))
                    log.info(f'[SIM] Using real ATR={atr:.2f}')
                    return atr
        except Exception:
            pass
        log.info(f'[SIM] Using SIM_ATR={config.SIM_ATR}')
        return config.SIM_ATR

    rates = mt5.copy_rates_from_pos(
        sym, mt5.TIMEFRAME_M1, 1, config.ATR_PERIOD + 3
    )
    if rates is None or len(rates) < 3:
        return 0.0
    df = pd.DataFrame(rates)
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )
    if len(tr) < config.ATR_PERIOD:
        return 0.0
    return float(np.mean(tr[-config.ATR_PERIOD:]))


def _best_fill() -> int:
    info = mt5.symbol_info(config.SYMBOL)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


# ─── Market Intelligence signals ─────────────────────────────────────────────

def _get_orderbook_bias(sym: str = None) -> dict:
    """
    Read MT5 Level 2 (market depth) and compute bid vs ask volume imbalance.
    Ratio > 2.0 = buyers dominating → BUY bias
    Ratio < 0.5 = sellers dominating → SELL bias
    Returns: {'bias': 'BUY'|'SELL'|'NEUTRAL', 'bid_vol', 'ask_vol', 'ratio', 'available'}
    """
    sym = sym or _sym()
    if config.SIMULATE:
        return {'bias': 'NEUTRAL', 'bid_vol': 0, 'ask_vol': 0, 'ratio': 1.0, 'available': False}
    try:
        mt5.market_book_add(sym)
        time.sleep(0.05)
        book_data = mt5.market_book_get(sym)
        mt5.market_book_release(sym)

        if not book_data:
            return {'bias': 'NEUTRAL', 'bid_vol': 0, 'ask_vol': 0, 'ratio': 1.0, 'available': False}

        bid_vol = sum(item.volume for item in book_data if item.type == mt5.BOOK_TYPE_BUY)
        ask_vol = sum(item.volume for item in book_data if item.type == mt5.BOOK_TYPE_SELL)

        if ask_vol == 0:
            ratio = 5.0
        elif bid_vol == 0:
            ratio = 0.0
        else:
            ratio = bid_vol / ask_vol

        bias = 'NEUTRAL'
        if ratio >= 2.0:
            bias = 'BUY'
        elif ratio <= 0.5:
            bias = 'SELL'

        return {
            'bias'     : bias,
            'bid_vol'  : round(bid_vol, 2),
            'ask_vol'  : round(ask_vol, 2),
            'ratio'    : round(ratio, 2),
            'available': True,
        }
    except Exception as e:
        log.debug(f'[OrderBook] error: {e}')
        return {'bias': 'NEUTRAL', 'bid_vol': 0, 'ask_vol': 0, 'ratio': 1.0, 'available': False}


def _get_tick_velocity(sym: str = None) -> dict:
    """
    Count how many ticks arrived in the last 10 seconds.
    HIGH velocity (≥20/s) = a move is in progress.
    LOW velocity (<2/s) = market sleeping, not a great entry time.
    Returns: {'ticks_per_sec': float, 'velocity': 'HIGH'|'NORMAL'|'LOW', 'available': bool}
    """
    sym = sym or _sym()
    if config.SIMULATE:
        return {'ticks_per_sec': 5.0, 'velocity': 'NORMAL', 'available': False}
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from_utc = _dt.now(_tz.utc) - _td(seconds=10)
        ticks = mt5.copy_ticks_from(sym, from_utc, 500, mt5.COPY_TICKS_ALL)

        if ticks is None or len(ticks) == 0:
            return {'ticks_per_sec': 0.0, 'velocity': 'LOW', 'available': True}

        tps = len(ticks) / 10.0
        velocity = 'HIGH' if tps >= 20 else ('LOW' if tps < 2 else 'NORMAL')
        return {'ticks_per_sec': round(tps, 1), 'velocity': velocity, 'available': True}
    except Exception as e:
        log.debug(f'[TickVelocity] error: {e}')
        return {'ticks_per_sec': 0.0, 'velocity': 'NORMAL', 'available': False}


def _get_spread_ratio(sym: str = None) -> dict:
    """
    Compare current spread to its 60-second rolling average.
    ratio ≥ 2.5 = spread anomaly: something unusual is happening (big player moving).
    Returns: {'spread', 'avg_spread', 'ratio', 'anomaly', 'available'}
    """
    sym = sym or _sym()
    if config.SIMULATE:
        return {'spread': 0.26, 'avg_spread': 0.26, 'ratio': 1.0, 'anomaly': False, 'available': False}
    try:
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return {'spread': 0, 'avg_spread': 0, 'ratio': 1.0, 'anomaly': False, 'available': False}

        current = round(tick.ask - tick.bid, 2)

        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from_utc = _dt.now(_tz.utc) - _td(seconds=60)
        hist = mt5.copy_ticks_from(sym, from_utc, 200, mt5.COPY_TICKS_ALL)

        if hist is not None and len(hist) >= 5:
            spreads   = [round(float(t['ask']) - float(t['bid']), 2)
                         for t in hist if float(t['ask']) > 0 and float(t['bid']) > 0]
            avg_spread = round(sum(spreads) / len(spreads), 2) if spreads else current
        else:
            avg_spread = current

        ratio   = round(current / avg_spread, 2) if avg_spread > 0 else 1.0
        anomaly = ratio >= 2.5

        return {
            'spread'    : current,
            'avg_spread': avg_spread,
            'ratio'     : ratio,
            'anomaly'   : anomaly,
            'available' : True,
        }
    except Exception as e:
        log.debug(f'[SpreadRatio] error: {e}')
        return {'spread': 0, 'avg_spread': 0, 'ratio': 1.0, 'anomaly': False, 'available': False}


def get_market_intelligence(sym: str = None) -> dict:
    """
    Combine order book bias, tick velocity, and spread anomaly into a single
    pre-entry snapshot. Logged on every /open call. Advisory only — does not
    block cycle open, but gives the engine rich context for future decisions.
    """
    sym = sym or _sym()
    ob  = _get_orderbook_bias(sym)
    tv  = _get_tick_velocity(sym)
    sr  = _get_spread_ratio(sym)
    return {
        'orderbook': ob,
        'tick_vel' : tv,
        'spread'   : sr,
        'summary'  : (
            f'OB={ob["bias"]}({ob["ratio"]}x) '
            f'Ticks={tv["ticks_per_sec"]}/s[{tv["velocity"]}] '
            f'Spread={sr["spread"]}x{sr["ratio"]}{"⚠" if sr["anomaly"] else ""}'
        ),
    }


def _in_hours() -> bool:
    """
    Returns True if we're allowed to open a new cycle right now.
    - Simulate mode: always True
    - Schedule mode: always True (BTC covers nights + weekends)
    - Single-symbol: check GOLD_START_HOUR – GOLD_END_HOUR window
    """
    if config.SIMULATE or config.ENABLE_SCHEDULE:
        return True
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('Europe/London'))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=1)))
    return config.GOLD_START_HOUR <= now.hour < config.GOLD_END_HOUR


def _cooldown_secs() -> int:
    """Returns the appropriate cooldown duration for the current mode."""
    return config.SIM_COOLDOWN_SECS if config.SIMULATE else config.COOLDOWN_SECS


# ─── Compute SL / TP ─────────────────────────────────────────────────────────

def compute_sl_tp(atr: float, profile: dict | None = None) -> tuple[float, float]:
    """Return (sl_dist, tp_extra) both clamped to the active profile's limits."""
    sl_min  = profile['sl_min']       if profile else config.SL_MIN
    sl_max  = profile['sl_max']       if profile else config.SL_MAX
    tp_min  = profile['tp_extra_min'] if profile else config.TP_EXTRA_MIN
    tp_max  = profile['tp_extra_max'] if profile else config.TP_EXTRA_MAX
    sl_dist  = round(max(sl_min, min(atr * config.SL_ATR_MULT,   sl_max)), 2)
    tp_extra = round(max(tp_min, min(atr * config.TP_EXTRA_MULT, tp_max)), 2)
    return sl_dist, tp_extra


# ─── Vantage hedge execution ─────────────────────────────────────────────────

def _open_vantage_hedge(side: str, lot: float,
                         sl: float = 0.0, tp: float = 0.0) -> int | None:
    """
    Open a hedge position on Vantage via MT5 with optional server-side SL/TP.
    Server-side SL/TP lets MT5 close instantly without Python polling delay.
    Simulate mode: returns a fake ticket — no real order sent.
    Returns ticket number or None on failure.
    """
    if config.SIMULATE:
        fake = random.randint(10000, 99999)
        log.info(f'[SIM] Fake Vantage hedge {side} opened: ticket={fake} @ {_sim["mid"]:.2f}')
        return fake

    sym = _sym()
    if not _ensure_symbol(sym):
        return None
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return None

    order_type = mt5.ORDER_TYPE_SELL if side == 'SELL' else mt5.ORDER_TYPE_BUY
    price      = round(tick.bid if side == 'SELL' else tick.ask, 2)

    req = {
        'action'       : mt5.TRADE_ACTION_DEAL,
        'symbol'       : sym,
        'volume'       : lot,
        'type'         : order_type,
        'price'        : price,
        'deviation'    : config.LOCK_DEVIATION,
        'magic'        : config.HEDGE_MAGIC,
        'comment'      : f'ACB-HEDGE-{side}',
        'type_time'    : mt5.ORDER_TIME_GTC,
    }
    # Attach server-side SL/TP so MT5 closes instantly at these levels
    if sl > 0:
        req['sl'] = round(sl, 2)
    if tp > 0:
        req['tp'] = round(tp, 2)

    for fill in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
        req['type_filling'] = fill
        res = mt5.order_send(req)
        if res and res.retcode == 10030:
            continue   # unsupported fill — try next
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                f'Hedge {side} opened: ticket={res.order} price={price:.2f}'
                + (f' SL={sl:.2f}' if sl else '')
                + (f' TP={tp:.2f}' if tp else '')
            )
            return res.order
        if res:
            log.error(f'Hedge open fail: retcode={res.retcode}')
            if res.retcode == 10027:
                log.error('MT5 Algo Trading is DISABLED — click the green button in MT5 toolbar!')
                _tg_alert(
                    '🔴 <b>MT5 Algo Trading DISABLED</b>\n'
                    'Hedge order rejected (retcode=10027).\n'
                    'Open MT5 -> click the green <b>Algo Trading</b> button in toolbar NOW.'
                )
            return None
    return None


def _close_vantage_hedge(ticket: int, side: str, lot: float) -> float | None:
    """
    Close a Vantage hedge by ticket. Returns close price or None.
    Simulate mode: returns simulated close price with approximate spread applied.
    """
    if config.SIMULATE:
        spread = 0.13   # half of Vantage $0.26 spread
        # For a SELL hedge closing at ask = mid + half_spread
        # For a BUY hedge closing at bid = mid - half_spread
        close = round(_sim['mid'] + (spread if side == 'SELL' else -spread), 2)
        log.info(f'[SIM] Fake Vantage hedge {side} closed: ticket={ticket} @ {close:.2f}')
        return close

    sym  = _sym()
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return None
    close_type  = mt5.ORDER_TYPE_BUY  if side == 'SELL' else mt5.ORDER_TYPE_SELL
    close_price = round(tick.ask if side == 'SELL' else tick.bid, 2)

    for fill in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
        res = mt5.order_send({
            'action'       : mt5.TRADE_ACTION_DEAL,
            'symbol'       : sym,
            'volume'       : lot,
            'type'         : close_type,
            'price'        : close_price,
            'position'     : ticket,
            'deviation'    : config.LOCK_DEVIATION,
            'magic'        : config.HEDGE_MAGIC,
            'comment'      : 'ACB-HEDGE-CLOSE',
            'type_time'    : mt5.ORDER_TIME_GTC,
            'type_filling' : fill,
        })
        if res and res.retcode == 10030:
            continue
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f'Hedge closed: ticket={ticket} @ {close_price:.2f}')
            return close_price
        if res:
            log.error(f'Hedge close fail: retcode={res.retcode}')
            return None
    return None


# ─── Post-news 4-factor intelligence scorer ──────────────────────────────────

def _score_post_news(ns: dict) -> dict:
    """
    Score market readiness for post-news entry.
    Evaluates 4 factors using live MT5 data + gravity cache + headline sentiment.

    Factors:
      1. Price moved $3+ in one direction AND held (not spike-reversed)
      2. Gravity is FIRE with score ≥ 4/5
      3. Headline sentiment is directional (BULLISH or BEARISH, not MIXED/NEUTRAL)
      4. Spread ≤ $1.00 (market chaos settled, market makers calm)

    Returns {'passed': bool, 'total': int, 'summary': str, 'details': dict}
    Fail-open: any individual factor error counts as 0 for that factor only.
    """
    total   = 0
    details = {}

    # ── Factor 1: Price movement ───────────────────────────────────────────────
    try:
        import MetaTrader5 as _mt5
        rates = _mt5.copy_rates_from_pos(_sym(), _mt5.TIMEFRAME_M1, 0, 4)
        if rates is not None and len(rates) >= 3:
            # Compare open of oldest bar to close of newest bar
            oldest_open   = float(rates[0]['open'])
            newest_close  = float(rates[-1]['close'])
            move          = abs(newest_close - oldest_open)
            # Also check it's not a spike-reverse: the last close should be
            # in the same direction as the overall move (not back to start)
            mid_close = float(rates[len(rates)//2]['close'])
            consistent = (
                (newest_close > oldest_open and mid_close > oldest_open) or
                (newest_close < oldest_open and mid_close < oldest_open)
            )
            if move >= 3.0 and consistent:
                total += 1
                details['price'] = f'${move:.2f} sustained ✓'
            elif move >= 3.0:
                details['price'] = f'${move:.2f} but spike-reversed ✗'
            else:
                details['price'] = f'${move:.2f} (need ${3.0}+) ✗'
        else:
            details['price'] = 'no rates ✗'
    except Exception as _e:
        details['price'] = f'error ✗'
        log.debug(f'[News] Score factor1 error: {_e}')

    # ── Factor 2: Gravity strength ─────────────────────────────────────────────
    try:
        gc = _gravity_cache
        if gc['verdict'] == 'FIRE' and gc.get('score', 0) >= 4:
            total += 1
            details['gravity'] = f'FIRE {gc["score"]}/5 ✓'
        else:
            details['gravity'] = f'{gc["verdict"]} {gc.get("score", 0)}/5 ✗'
    except Exception as _e:
        details['gravity'] = 'error ✗'
        log.debug(f'[News] Score factor2 error: {_e}')

    # ── Factor 3: Headline sentiment ───────────────────────────────────────────
    try:
        import news_filter as _nf
        sentiment = ns.get('sentiment') or _nf.get_headline_sentiment()
        if sentiment in ('BULLISH', 'BEARISH'):
            total += 1
            details['sentiment'] = f'{sentiment} ✓'
        else:
            details['sentiment'] = f'{sentiment} ✗'
    except Exception as _e:
        details['sentiment'] = 'error ✗'
        log.debug(f'[News] Score factor3 error: {_e}')

    # ── Factor 4: Spread normal ────────────────────────────────────────────────
    try:
        import MetaTrader5 as _mt5
        tick = _mt5.symbol_info_tick(_sym())
        if tick is not None:
            spread = round(tick.ask - tick.bid, 2)
            import news_filter as _nf
            limit = _nf.SPREAD_LIMIT
            if spread <= limit:
                total += 1
                details['spread'] = f'${spread:.2f} ✓'
            else:
                details['spread'] = f'${spread:.2f} wide (>${limit}) ✗'
        else:
            details['spread'] = 'no tick ✗'
    except Exception as _e:
        details['spread'] = 'error ✗'
        log.debug(f'[News] Score factor4 error: {_e}')

    try:
        import news_filter as _nf
        min_score = _nf.ENTRY_SCORE_MIN
    except Exception:
        min_score = 3

    summary = ' | '.join(f'{k}: {v}' for k, v in details.items())
    return {
        'passed' : total >= min_score,
        'total'  : total,
        'summary': summary,
        'details': details,
    }


# ─── Public: called by broker.py /open endpoint ──────────────────────────────

def trigger_open_cycle() -> dict:
    """
    Connect to MT5, check ATR + hours, open an internal cycle, set BOTH_OPEN state.
    Called from the FastAPI request thread — MT5 connection is opened here then
    handed off (the engine thread reconnects when it needs price data).
    Returns a dict with cycle info, or {'error': '...'}.
    """
    if not config.CYCLES_ENABLED:
        log.info('[Engine] CYCLES_ENABLED=false — new cycle open blocked')
        return {'error': 'Cycles disabled (CYCLES_ENABLED=false)'}
    if not _in_hours():
        return {'error': 'Outside trading hours'}

    # ── News filter (economic calendar + live headlines) ──────────────────────
    if config.NEWS_FILTER_ENABLED and not config.SIMULATE:
        try:
            # Resolve news_filter.py relative to this package — works regardless
            # of the current working directory when the broker is started.
            import sys as _sys, os as _os
            _nf_search = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if _nf_search not in _sys.path:
                _sys.path.insert(0, _nf_search)
            import news_filter as _nf
            _ns = _nf.get_news_status()

            if _ns['status'] == 'CLEAR':
                log.debug('[News] CLEAR — proceeding')

            elif _ns['status'] == 'PRE_NEWS':
                # Scheduled event approaching — always pause, no scoring
                log.info(f'[News] PRE_NEWS: {_ns["reason"]}')
                return {'error': f'News PRE_NEWS: {_ns["reason"]}'}

            elif _ns['status'] in ('MONITORING', 'PAUSED'):
                # Event has fired — run 4-factor intelligence score
                if _ns.get('in_chaos', True):
                    # Still in 90s chaos window — never enter
                    log.info(f'[News] Chaos window active: {_ns["reason"]}')
                    return {'error': f'News MONITORING: {_ns["reason"]}'}

                score = _score_post_news(_ns)
                if score['passed']:
                    log.info(
                        f'[News] MONITORING score {score["total"]}/4 — '
                        f'entering with confidence | {score["summary"]}'
                    )
                    # Proceed through to open the cycle
                else:
                    log.info(
                        f'[News] MONITORING score {score["total"]}/4 — '
                        f'waiting | {score["summary"]}'
                    )
                    return {
                        'error': (
                            f'News MONITORING: {_ns["reason"]} '
                            f'[score {score["total"]}/4: {score["summary"]}]'
                        )
                    }

        except ImportError as _nie:
            # news_filter.py not found — this is NOT a silent fail.
            # Alert the operator immediately so it gets fixed.
            log.error(f'[News] CRITICAL — news_filter.py not found: {_nie}')
            if not getattr(trigger_open_cycle, '_nf_alert_sent', False):
                _tg_alert(
                    '🚨 <b>News filter NOT running</b>\n'
                    'news_filter.py could not be loaded.\n'
                    'System is trading <b>WITHOUT</b> news protection!\n\n'
                    'Fix: upload news_filter.py to the broker root folder '
                    '(same level as the autocycle_broker/ directory), then restart.'
                )
                trigger_open_cycle._nf_alert_sent = True
        except Exception as _ne:
            log.debug(f'[News] filter error (fail-open): {_ne}')

    # ── Level Gravity filter (instant — reads cached result, no MT5 call) ──────
    if config.GRAVITY_FILTER_ENABLED and not config.SIMULATE:
        gc  = _gravity_cache
        age = time.time() - gc['updated_at']
        if age > config.GRAVITY_STALE_SECS:
            # Gravity data too old (MT5 may have been offline) — bypass filter
            log.warning(f'Gravity data stale ({age:.0f}s) — opening without direction filter')
        elif gc['verdict'] != 'FIRE' or gc.get('score', 0) < 3:
            score = gc.get('score', 0)
            if gc['verdict'] == 'FIRE' and score < 3:
                reason = f'momentum too weak — only {score}/5 votes, minimum 3 required'
            else:
                reason = gc.get('skip_reason') or 'no clear directional bias'
            log.info(f'Gravity SKIP — cycle blocked: {reason} (score {score}/5)')
            return {'error': f'Gravity SKIP: {score}/5 — {reason}'}
        else:
            grav_dir   = gc.get('direction')
            grav_score = gc.get('score', 0)
            log.info(
                f'Gravity FIRE ✓ dir={grav_dir} score={grav_score}/5 '
                f'— proceeding with cycle open'
            )

            # ── Currency bias confirmation ────────────────────────────────────
            # If a recent EURUSD/GBPUSD gap set a Gold bias, use it.
            # Bias CONFIRMS gravity → bonus log, open freely.
            # Bias OPPOSES gravity  → require stronger gravity (4/5) before opening.
            # No active bias → no change, proceed normally.
            try:
                import sys as _sys, os as _os
                _gsp = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                if _gsp not in _sys.path:
                    _sys.path.insert(0, _gsp)
                from gap_scanner import get_gold_bias
                _cb = get_gold_bias()
                if _cb['active'] and grav_dir:
                    if _cb['direction'] == grav_dir:
                        log.info(
                            f'[CurrencyBias] ✓ {_cb["source"]} confirms {grav_dir} '
                            f'({_cb["age_secs"]}s ago) — extra confidence'
                        )
                    else:
                        # Log the conflict as context — don't block, gravity wins
                        log.info(
                            f'[CurrencyBias] {_cb["source"]} says {_cb["direction"]} '
                            f'but gravity {grav_dir} at {grav_score}/5 — gravity wins, opening'
                        )
            except Exception as _cbe:
                log.debug(f'[CurrencyBias] check error (fail-open): {_cbe}')

    # Pick the right instrument profile for this moment
    profile = get_active_profile()
    sym     = profile['symbol']

    if not _mt5_connect():
        return {'error': 'MT5 connection failed'}
    if not _ensure_symbol(sym):
        return {'error': f'Symbol {sym} not available'}

    atr = _get_atr(sym=sym)

    if atr < profile['atr_min']:
        return {'error': f'ATR {atr:.2f} below minimum {profile["atr_min"]} — market too flat'}

    # ── Market intelligence — reads live data, gates the trade ─────────────────
    if not config.SIMULATE:
        try:
            _mi = get_market_intelligence(sym)
            log.info(f'[MarketIntel] {_mi["summary"]}')

            # BLOCK: Extreme spread anomaly only (4x+ = genuine market chaos/crash)
            # Normal London/NY session volatility never reaches 4x.
            # This only fires during flash crashes, major news spikes, broker issues.
            if _mi['spread']['available']:
                _sp  = _mi['spread']['spread']
                _avg = _mi['spread']['avg_spread']
                _rx  = _mi['spread']['ratio']
                if _rx >= 4.0:
                    log.info(f'[MarketIntel] BLOCK — extreme spread ${_sp:.2f} = {_rx}x avg (crash/chaos)')
                    return {
                        'error': (
                            f'Extreme spread anomaly: ${_sp:.2f} is {_rx}x normal — '
                            f'market in crash/chaos, skip'
                        )
                    }
                # Log OB and velocity as intelligence (not blocking — just context)
                log.info(f'[MarketIntel] {_mi["summary"]}')

        except Exception as _mie:
            log.debug(f'[MarketIntel] error (fail-open): {_mie}')

    # Get stable mid price in the SAME session — no reconnect needed.
    # Read repeatedly until two consecutive readings agree within 0.1%
    # to avoid acting on a stale cached tick.
    mid = None
    for _ in range(10):
        time.sleep(0.5)
        price = _get_mid()
        if price is None or price <= 0:
            continue
        if mid is not None and mid > 0 and abs(price - mid) / mid < 0.001:
            mid = price
            break
        mid = price
    # Leave MT5 connected — monitoring loop reuses the existing session

    if mid is None or mid <= 0:
        return {'error': 'No price tick available'}

    sl_dist, tp_extra = compute_sl_tp(atr, profile)
    lot = book.lot_for_risk(sl_dist, profile['contract_size'])

    cycle_id = book.open_cycle(
        lot, atr, sl_dist, tp_extra, mid,
        symbol=profile['symbol'],
        contract_size=profile['contract_size'],
    )

    # In simulate mode: reset the synthetic price to the opening mid
    if config.SIMULATE:
        _sim['mid']  = mid
        _sim['step'] = 0.0
        log.info(f'[SIM] Price reset to {mid:.2f} — drifting toward BUY SL over ~12s')

    _sm({
        'phase'           : 'BOTH_OPEN',
        'cycle_id'        : cycle_id,
        'atr'             : round(atr, 2),
        'sl_dist'         : sl_dist,
        'tp_extra'        : tp_extra,
        'mid'             : mid,
        'error'           : None,
        'active_symbol'   : profile['symbol'],
        'active_contract' : profile['contract_size'],
        'active_rev_tol'  : profile['rev_tol'],
        'active_guard_tol': profile['guard_tol'],
        'both_open_since' : time.time(),
    })

    log.info(
        f'[CYCLE {cycle_id}] OPEN | sym={sym} mid={mid:.2f} lot={lot} '
        f'ATR={atr:.2f} SL±{sl_dist:.2f} TP+{tp_extra:.2f}'
        + (' [SIMULATE]' if config.SIMULATE else '')
    )
    return {
        'cycle_id'    : cycle_id,
        'symbol'      : sym,
        'mid'         : mid,
        'lot'         : lot,
        'atr'         : round(atr, 2),
        'sl_dist'     : sl_dist,
        'tp_extra'    : tp_extra,
        'buy_sl'      : round(mid - sl_dist, 2),
        'sell_sl'     : round(mid + sl_dist, 2),
        'tp_target_if_buy_sl_fires' : round(mid - sl_dist - tp_extra, 2),
        'tp_target_if_sell_sl_fires': round(mid + sl_dist + tp_extra, 2),
    }


# ─── Engine monitoring loop ───────────────────────────────────────────────────

def run_engine():
    log.info('Engine thread running')
    mt5_ok = False
    _account_check_ts = 0.0   # tracks last periodic account verification

    while True:
        try:
            with _lock:
                phase    = _state['phase']
                cycle_id = _state['cycle_id']

            # ── IDLE / COOLDOWN — no MT5 needed ──────────────────────────────
            if phase == 'IDLE':
                # Keep MT5 connected while idle so the gravity loop has live tick data.
                if not mt5_ok:
                    mt5_ok = _mt5_connect() and _ensure_symbol()
                    if mt5_ok:
                        _account_check_ts = time.time()
                    else:
                        time.sleep(5)
                        continue
                else:
                    # Re-verify account every 30s — catches live → demo drift
                    # (the live account connection can drop back to demo silently)
                    _now = time.time()
                    if _now - _account_check_ts > 30:
                        _cur = mt5.account_info()
                        if _cur is not None and _cur.login != config.MT5_LOGIN:
                            log.warning(
                                f'MT5 drifted to #{_cur.login} ({_cur.server}) — '
                                f'reconnecting to #{config.MT5_LOGIN}'
                            )
                            mt5_ok = False
                            continue
                        _account_check_ts = _now
                time.sleep(1)
                continue

            if phase == 'COOLDOWN':
                with _lock:
                    until = _state['cooldown_until']
                if time.time() >= until:
                    _s('phase', 'IDLE')
                    log.info('Cooldown complete → IDLE')
                time.sleep(1)
                continue

            # ── Active phases need MT5 price feed ─────────────────────────────
            if not mt5_ok:
                mt5_ok = _mt5_connect() and _ensure_symbol()
                if not mt5_ok:
                    log.warning('MT5 not ready — retrying in 5s')
                    time.sleep(5)
                    continue

            mid = _get_mid()
            if mid is None:
                mt5_ok = False
                time.sleep(config.SCAN_INTERVAL)
                continue

            _s('mid', mid)

            # ── BOTH_OPEN ─────────────────────────────────────────────────────
            if phase == 'BOTH_OPEN':
                positions = book.get_open_positions(cycle_id)
                buy_pos  = next((p for p in positions if p['side'] == 'BUY'),  None)
                sell_pos = next((p for p in positions if p['side'] == 'SELL'), None)

                if not buy_pos or not sell_pos:
                    time.sleep(config.SCAN_INTERVAL)
                    continue

                # ── Straddle timeout: flat market, SL never fired ─────────────
                with _lock:
                    both_open_since = _state.get('both_open_since', time.time())
                if time.time() - both_open_since > config.MAX_STRADDLE_WAIT_SECS:
                    for pos in positions:
                        book.close_position(pos['id'], mid, 'TIMEOUT')
                    book.close_cycle(cycle_id, 'TIMEOUT', 0.0, 0.0)
                    # If an early hedge was already open on Tickmill, close it now
                    # before going to COOLDOWN — otherwise it stays orphaned.
                    with _lock:
                        _orphan_ticket = _state.get('_early_hedge_ticket')
                        _orphan_side   = _state.get('_early_hedge_side')
                        _orphan_lot    = buy_pos['lot']
                    if _orphan_ticket and _orphan_side and not config.SIMULATE:
                        _close_vantage_hedge(_orphan_ticket, _orphan_side, _orphan_lot)
                        log.info(
                            f'[EarlyHedge] Closed orphaned ticket={_orphan_ticket} '
                            f'on straddle timeout'
                        )
                    log.info(
                        f'[CYCLE {cycle_id}] STRADDLE TIMEOUT — neither SL fired in '
                        f'{config.MAX_STRADDLE_WAIT_SECS}s — restarting after cooldown'
                    )
                    _sm({
                        'phase'              : 'COOLDOWN',
                        'cooldown_until'     : time.time() + _cooldown_secs(),
                        '_early_hedge_ticket': None,
                        '_early_hedge_side'  : None,
                        '_early_hedge_price' : 0.0,
                    })
                    continue

                # ── Early hedge: open Tickmill position before SL fires ───────
                # When price is within EARLY_HEDGE_THRESHOLD of a SL AND order
                # book + velocity confirm direction, push the hedge to Tickmill
                # NOW — capturing those last 0.20 pts as bonus profit on top of
                # tp_extra. When the internal SL fires moments later, the hedge
                # is already open and already in profit.
                with _lock:
                    _eh_ticket = _state.get('_early_hedge_ticket')
                    _tp_extra  = _state.get('tp_extra', 0.0)

                # ── Verify early hedge is still alive (its SL may have fired) ──
                # Tickmill closes positions server-side — we only know by checking.
                # If the ticket is gone, clear state so normal hedge opens on SL.
                if _eh_ticket and not config.SIMULATE:
                    _live = mt5.positions_get(ticket=_eh_ticket)
                    if _live is None or len(_live) == 0:
                        log.info(
                            f'[EarlyHedge] SL fired on Tickmill (ticket={_eh_ticket}) '
                            f'— cleared, normal hedge will open when internal SL fires'
                        )
                        _sm({
                            '_early_hedge_ticket': None,
                            '_early_hedge_side'  : None,
                            '_early_hedge_price' : 0.0,
                        })
                        _eh_ticket = None

                if not _eh_ticket and not config.SIMULATE:
                    _thr      = config.EARLY_HEDGE_THRESHOLD
                    _buy_gap  = mid - buy_pos['sl']    # pts above BUY SL (>0 = not hit)
                    _sell_gap = sell_pos['sl'] - mid   # pts below SELL SL (>0 = not hit)

                    _near_side = None
                    if 0 < _buy_gap <= _thr:
                        _near_side = 'BUY'    # BUY SL about to fire
                    elif 0 < _sell_gap <= _thr:
                        _near_side = 'SELL'   # SELL SL about to fire

                    if _near_side:
                        _sym_now = _sym()
                        _ob = _get_orderbook_bias(_sym_now)
                        _tv = _get_tick_velocity(_sym_now)
                        # OB must agree with the expected move direction:
                        # BUY SL about to fire = price going DOWN = expect SELL dominance
                        # SELL SL about to fire = price going UP   = expect BUY dominance
                        _expected_ob   = 'SELL' if _near_side == 'BUY' else 'BUY'
                        _ob_ok  = _ob['available'] and _ob['bias'] == _expected_ob
                        _vel_ok = _tv['available'] and _tv['velocity'] == 'HIGH'

                        if _ob_ok and _vel_ok:
                            _eh_side = 'SELL' if _near_side == 'BUY' else 'BUY'
                            _eh_lot  = (buy_pos['lot'] if _near_side == 'BUY'
                                        else sell_pos['lot'])
                            # TP anchored to the expected SL price (where internal
                            # SL will fire) plus tp_extra — not current mid.
                            _sl_ref = (buy_pos['sl']  if _near_side == 'BUY'
                                       else sell_pos['sl'])
                            _eh_tp  = round(
                                _sl_ref - _tp_extra if _eh_side == 'SELL'
                                else _sl_ref + _tp_extra, 2
                            )
                            # Tight SL on the early hedge: 1.5× threshold in the
                            # wrong direction — closes at tiny loss if price reverses.
                            _eh_sl = round(
                                mid + _thr * 1.5 if _eh_side == 'SELL'
                                else mid - _thr * 1.5, 2
                            )
                            _eh_ticket = _open_vantage_hedge(
                                _eh_side, _eh_lot, sl=_eh_sl, tp=_eh_tp
                            )
                            if _eh_ticket:
                                _sm({
                                    '_early_hedge_ticket': _eh_ticket,
                                    '_early_hedge_side'  : _eh_side,
                                    '_early_hedge_price' : mid,
                                })
                                log.info(
                                    f'[EarlyHedge] ✅ {_eh_side} opened {_thr:.2f}pts '
                                    f'early at {mid:.2f} | SL fires ~{_sl_ref:.2f} | '
                                    f'TP={_eh_tp:.2f} SL={_eh_sl:.2f} | '
                                    f'OB={_ob["bias"]} vel={_tv["velocity"]}'
                                )

                sl_fired = None
                if   mid <= buy_pos['sl']:
                    sl_fired = 'BUY'
                elif mid >= sell_pos['sl']:
                    sl_fired = 'SELL'

                if not sl_fired:
                    time.sleep(config.SCAN_INTERVAL)
                    continue

                # ── SL fired ─────────────────────────────────────────────────
                fired_pos    = buy_pos  if sl_fired == 'BUY'  else sell_pos
                survivor_pos = sell_pos if sl_fired == 'BUY'  else buy_pos
                sl_pnl       = book.close_position(fired_pos['id'], mid, 'SL')
                sl_price     = mid

                log.info(
                    f'[CYCLE {cycle_id}] {sl_fired} SL fired at {mid:.2f} | '
                    f'internal={sl_pnl:+.2f}'
                )

                mid2 = mid

                # Open Vantage hedge (or reuse early hedge if already open)
                hedge_side = 'SELL' if sl_fired == 'BUY' else 'BUY'
                lot        = survivor_pos['lot']

                with _lock:
                    _tp_extra       = _state.get('tp_extra', 0.0)
                    _eh_ticket      = _state.get('_early_hedge_ticket')
                    _eh_side        = _state.get('_early_hedge_side')
                    _eh_open_price  = _state.get('_early_hedge_price', 0.0)

                # ── Reuse early hedge if it was opened on the correct side ──────
                # The early hedge was pushed to Tickmill 0.20 pts ago — it has
                # already captured that bonus movement. Skip re-opening; just
                # hand its ticket to SURVIVOR.
                if _eh_ticket and _eh_side == hedge_side:
                    ticket   = _eh_ticket
                    bonus    = round(abs(mid - _eh_open_price), 2)
                    hedge_tp = None   # TP was already set on the early hedge order
                    log.info(
                        f'[EarlyHedge] ♻️  Reusing ticket={ticket} '
                        f'(opened {bonus:.2f}pts early at {_eh_open_price:.2f}) — '
                        f'SURVIVOR starts with {bonus:.2f}pts bonus profit'
                    )
                else:
                    # Normal path: SL fired without early hedge — open now.
                    # CRITICAL: calculate TP from actual fill price (ask/bid),
                    # NOT from sl_price (mid) — otherwise gap to TP can be < spread
                    # and Vantage silently ignores the TP, leaving position naked.
                    _tick_pre = mt5.symbol_info_tick(_sym()) if not config.SIMULATE else None
                    if _tick_pre is not None:
                        _fill_ref = round(
                            _tick_pre.ask if hedge_side == 'BUY' else _tick_pre.bid, 2
                        )
                    else:
                        _fill_ref = sl_price

                    hedge_tp = (round(_fill_ref + _tp_extra, 2) if hedge_side == 'BUY'
                                else round(_fill_ref - _tp_extra, 2))

                    ticket = None
                    for _attempt in range(3):
                        ticket = _open_vantage_hedge(hedge_side, lot, sl=0, tp=hedge_tp)
                        if ticket is not None:
                            break
                    log.warning(
                        f'[CYCLE {cycle_id}] Hedge open attempt {_attempt + 1}/3 failed'
                        + (' — retrying after reconnect' if _attempt < 2 else ' — giving up')
                    )
                    if _attempt < 2:
                        time.sleep(1)
                        mt5_ok = _mt5_connect() and _ensure_symbol(_sym())
                        # Refresh tick-based TP reference after reconnect
                        _tick_retry = mt5.symbol_info_tick(_sym()) if not config.SIMULATE else None
                        if _tick_retry:
                            _fill_ref2 = round(
                                _tick_retry.ask if hedge_side == 'BUY' else _tick_retry.bid, 2
                            )
                            hedge_tp = round(
                                (_fill_ref2 + _tp_extra) if hedge_side == 'BUY'
                                else (_fill_ref2 - _tp_extra), 2
                            )

                # Verify TP was accepted by broker — ECN brokers sometimes silently
                # reject TP on TRADE_ACTION_DEAL if the distance is too small.
                # Fix it immediately with a TRADE_ACTION_SLTP modification.
                if ticket is not None and not config.SIMULATE:
                    time.sleep(0.3)
                    _pos_check = mt5.positions_get(ticket=ticket)
                    if _pos_check and len(_pos_check) > 0:
                        _set_tp = _pos_check[0].tp
                        if abs(_set_tp - hedge_tp) > 0.05:
                            log.warning(
                                f'[CYCLE {cycle_id}] Hedge TP not set by broker '
                                f'(got {_set_tp:.2f}, expected {hedge_tp:.2f}) — '
                                f'applying via SLTP modification'
                            )
                            _sltp_req = {
                                'action'  : mt5.TRADE_ACTION_SLTP,
                                'symbol'  : _sym(),
                                'position': ticket,
                                'tp'      : round(hedge_tp, 2),
                                'sl'      : 0.0,
                                'magic'   : config.HEDGE_MAGIC,
                            }
                            _sltp_res = mt5.order_send(_sltp_req)
                            if _sltp_res and _sltp_res.retcode == mt5.TRADE_RETCODE_DONE:
                                log.info(f'[CYCLE {cycle_id}] SLTP fix applied — TP={hedge_tp:.2f}')
                            else:
                                log.error(
                                    f'[CYCLE {cycle_id}] SLTP fix failed: '
                                    f'{_sltp_res.retcode if _sltp_res else "no response"}'
                                )

                if ticket is None:
                    # Hedge failed — close survivor at current price, take breakeven
                    surv_pnl = book.close_position(survivor_pos['id'], mid2, 'REVERSAL')
                    gross    = round(sl_pnl + surv_pnl, 2)
                    book.close_cycle(cycle_id, 'BREAKEVEN', gross, 0.0)
                    log.warning(f'[CYCLE {cycle_id}] Hedge failed — closing at breakeven')
                    _sm({
                        'phase'              : 'COOLDOWN',
                        'cooldown_until'     : time.time() + _cooldown_secs(),
                        '_early_hedge_ticket': None,
                        '_early_hedge_side'  : None,
                        '_early_hedge_price' : 0.0,
                    })
                    continue  # MT5 stays connected

                # Hedge open — record it and move to SURVIVOR phase
                tick_now = mt5.symbol_info_tick(_sym())
                hedge_open_price = round(
                    (tick_now.bid if hedge_side == 'SELL' else tick_now.ask), 2
                ) if tick_now else mid2

                book.open_hedge(cycle_id, ticket, hedge_side, lot, hedge_open_price)

                # TP target for Python-side monitoring — must match the server-side TP
                # that was placed on the order (calculated from fill price above).
                tp_target = hedge_tp

                _sm({
                    'phase'            : 'SURVIVOR',
                    'sl_fired_side'    : sl_fired,
                    'sl_price'         : sl_price,
                    'tp_target'        : tp_target,
                    'hedge_ticket'     : ticket,
                    '_survivor_id'     : survivor_pos['id'],
                    '_survivor_side'   : survivor_pos['side'],
                    '_survivor_lot'    : lot,
                    '_sl_pnl'            : sl_pnl,
                    '_hedge_open_ts'     : time.time(),
                    '_stall_since'       : 0.0,
                    '_intel_check_ts'    : 0.0,
                    # Clear early hedge fields — consumed, not needed in SURVIVOR
                    '_early_hedge_ticket': None,
                    '_early_hedge_side'  : None,
                    '_early_hedge_price' : 0.0,
                })
                log.info(
                    f'[CYCLE {cycle_id}] SURVIVOR | hedge={hedge_side} #{ticket} | '
                    f'TP target={tp_target:.2f} | waiting for TP — no guardian'
                )
                time.sleep(config.SCAN_INTERVAL)

            # ── SURVIVOR ──────────────────────────────────────────────────────
            elif phase == 'SURVIVOR':
                with _lock:
                    sl_fired       = _state['sl_fired_side']
                    sl_price       = _state['sl_price']
                    tp_target      = _state['tp_target']
                    survivor_id    = _state['_survivor_id']
                    survivor_side  = _state['_survivor_side']
                    lot            = _state['_survivor_lot']
                    sl_pnl         = _state['_sl_pnl']
                    hedge_ticket   = _state['hedge_ticket']
                    stall_since    = _state.get('_stall_since', 0.0)
                    intel_check_ts = _state.get('_intel_check_ts', 0.0)

                hedge_side = 'SELL' if sl_fired == 'BUY' else 'BUY'

                # ── Server-side TP check ──────────────────────────────────────
                server_closed = False
                if not config.SIMULATE and hedge_ticket:
                    _srv_pos = mt5.positions_get(ticket=hedge_ticket)
                    server_closed = (_srv_pos is not None and len(_srv_pos) == 0)

                # ── Python-side TP check (100ms backup) ──────────────────────
                if sl_fired == 'BUY':
                    tp_hit = server_closed or (mid <= tp_target)
                else:
                    tp_hit = server_closed or (mid >= tp_target)

                # ── Smart exit checks (every 5 seconds) ──────────────────────
                # Only runs when TP has NOT yet been hit.
                # Two triggers:
                #   EXIT 1 — Order book flips against hedge direction AND velocity
                #             is HIGH → real reversal, close immediately.
                #   EXIT 2 — Order book neutral AND velocity dead for 90s → market
                #             stalled, momentum is gone, exit now.
                smart_exit_reason = None
                _now = time.time()

                if not tp_hit and not config.SIMULATE and _now - intel_check_ts >= 1.0:
                    _s('_intel_check_ts', _now)
                    try:
                        _sym_now = _sym()
                        _ob = _get_orderbook_bias(_sym_now)
                        _tv = _get_tick_velocity(_sym_now)

                        _ob_bias  = _ob.get('bias', 'NEUTRAL')
                        _velocity = _tv.get('velocity', 'NORMAL')
                        _tps      = _tv.get('ticks_per_sec', 5.0)
                        _ob_avail = _ob.get('available', False)
                        _tv_avail = _tv.get('available', False)

                        if _ob_avail and _tv_avail:
                            _ob_against = (
                                (hedge_side == 'BUY'  and _ob_bias == 'SELL') or
                                (hedge_side == 'SELL' and _ob_bias == 'BUY')
                            )

                            # EXIT 1: real reversal
                            if _ob_against and _velocity == 'HIGH':
                                smart_exit_reason = (
                                    f'Reversal confirmed: OB={_ob_bias} vs '
                                    f'{hedge_side} hedge, velocity=HIGH ({_tps}/s)'
                                )

                            # EXIT 2: stall detection
                            if not smart_exit_reason:
                                _is_stalling = (_ob_bias == 'NEUTRAL'
                                                and _velocity == 'LOW')
                                if _is_stalling:
                                    if stall_since == 0.0:
                                        _s('_stall_since', _now)
                                    elif _now - stall_since >= 90.0:
                                        smart_exit_reason = (
                                            f'Stall: market dead {int(_now - stall_since)}s '
                                            f'(OB neutral, {_tps} ticks/s)'
                                        )
                                else:
                                    if stall_since != 0.0:
                                        _s('_stall_since', 0.0)  # reset — market moving again

                    except Exception as _se:
                        log.debug(f'[SmartExit] check error: {_se}')

                # ── Common close helper ───────────────────────────────────────
                def _do_close(reason: str, outcome: str, close_mid: float):
                    close_price = tp_target if server_closed else close_mid
                    surv_pnl    = book.close_position(survivor_id, close_price, reason)

                    if server_closed:
                        hcp = tp_target
                        log.info(
                            f'Hedge #{hedge_ticket} already closed server-side '
                            f'at TP={tp_target:.2f}'
                        )
                    else:
                        hcp = _close_vantage_hedge(hedge_ticket, survivor_side, lot)

                    v_pnl = book.close_hedge(cycle_id, hcp or close_mid)
                    gross = round(sl_pnl + surv_pnl, 2)
                    book.close_cycle(cycle_id, outcome, gross, v_pnl)
                    net = round(gross - config.COMMISSION, 2)
                    log.info(
                        f'[CYCLE {cycle_id}] {outcome} ({reason}) | '
                        f'internal net={net:+.2f} | Vantage={v_pnl:+.2f}'
                    )
                    _sm({'phase': 'COOLDOWN',
                         'cooldown_until': time.time() + _cooldown_secs()})

                # ── Decide: TP, smart exit, or keep watching ──────────────────
                if tp_hit:
                    _do_close(
                        'HEDGE_TP_SERVER' if server_closed else 'HEDGE_TP',
                        'TP',
                        mid,
                    )

                elif smart_exit_reason:
                    log.info(f'[CYCLE {cycle_id}] SMART EXIT → {smart_exit_reason}')
                    _do_close('SMART_EXIT', 'SMART_EXIT', mid)

                else:
                    time.sleep(config.SCAN_INTERVAL)
                # MT5 stays connected — no shutdown

            else:
                time.sleep(1)

        except Exception as exc:
            log.exception(f'Engine error: {exc}')
            try:
                mt5.shutdown()
            except Exception:
                pass
            mt5_ok = False
            time.sleep(3)


def _recover_on_startup():
    """
    Called once at startup.  If there is an unclosed cycle in the DB (meaning
    the broker was killed mid-trade), we try to close any open Vantage hedge
    gracefully and mark the cycle closed so the engine starts clean.

    Without this, a restarted broker would simply forget the hedge, leaving
    a live MT5 position open with no Python supervision.
    """
    cycle = book.get_current_cycle()
    if not cycle:
        return  # no open cycle — clean start

    cycle_id = cycle['id']
    log.warning(
        f'[RECOVERY] Found unclosed cycle {cycle_id} from before restart — '
        f'attempting cleanup'
    )

    hedge = book.get_open_hedge(cycle_id)
    positions = book.get_open_positions(cycle_id)

    # Try to connect MT5 and get current price
    price = None
    if not config.SIMULATE:
        try:
            if _mt5_connect() and _ensure_symbol():
                tick = mt5.symbol_info_tick(_sym())
                if tick:
                    price = round((tick.bid + tick.ask) / 2, 2)
        except Exception as e:
            log.warning(f'[RECOVERY] MT5 connect failed: {e}')

    if price is None:
        price = cycle.get('mid_entry') or 0.0

    # Close any open internal positions at current price
    for pos in positions:
        pnl = book.close_position(pos['id'], price, 'MANUAL')
        log.info(f'[RECOVERY] Closed internal position {pos["id"]} at {price:.2f} P&L={pnl:+.2f}')

    # Close Vantage hedge if one was open
    vantage_pnl = 0.0
    if hedge:
        if not config.SIMULATE:
            try:
                close_px = _close_vantage_hedge(
                    hedge['vantage_ticket'],
                    hedge['side'],
                    hedge['lot'],
                )
                vantage_pnl = book.close_hedge(cycle_id, close_px or price)
                log.info(
                    f'[RECOVERY] Closed Vantage hedge #{hedge["vantage_ticket"]} '
                    f'side={hedge["side"]} vantage_pnl={vantage_pnl:+.2f}'
                )
            except Exception as e:
                log.warning(f'[RECOVERY] Could not close Vantage hedge: {e}')
                book.close_hedge(cycle_id, price)
        else:
            book.close_hedge(cycle_id, price)

    # Mark the cycle closed
    book.close_cycle(cycle_id, 'MANUAL', 0.0, vantage_pnl)
    log.warning(f'[RECOVERY] Cycle {cycle_id} force-closed. Engine starting fresh.')

    _tg_alert(
        f'⚠️ <b>Broker Restarted Mid-Trade</b>\n'
        f'Cycle #{cycle_id} was auto-closed at recovery.\n'
        f'MT5 hedge was {"closed" if hedge else "not open"}.\n'
        f'Check MT5 manually to confirm no orphan positions.'
    )


def start_engine():
    _recover_on_startup()

    # ── News startup check ────────────────────────────────────────────────────
    if config.NEWS_FILTER_ENABLED:
        try:
            import news_filter as _nf
            _ns = _nf.startup_check()
            if _ns['status'] != 'CLEAR':
                _tg_alert(
                    f'📰 <b>Startup: news pause active</b>\n'
                    f'{_ns["reason"]}\n'
                    f'Trading will resume automatically when the window clears.'
                )
                log.warning(f'[News] Startup: {_ns["status"]} — {_ns["reason"]}')
            else:
                upcoming = _ns.get('upcoming', [])
                if upcoming:
                    _tg_alert(
                        f'⚠️ <b>Upcoming news events</b>\n'
                        + '\n'.join(f'• {u}' for u in upcoming[:5])
                        + '\n\nTrading will auto-pause before each event.'
                    )
                    log.info(f'[News] Startup CLEAR — events soon: {upcoming}')
                else:
                    log.info('[News] Startup: CLEAR — no events in next 30min')
        except Exception as _ne:
            log.debug(f'[News] Startup check error (ignored): {_ne}')

    # Connect MT5 before starting the gravity loop.
    # Without this, the gravity loop's first run happens before MT5 is ready,
    # sets verdict='SKIP' with reason='no tick data', and stamps a fresh timestamp.
    # Then /open bypasses the stale check but sees a fresh SKIP → blocks for 120s.
    log.info('Connecting MT5 before gravity loop...')
    if _mt5_connect() and _ensure_symbol():
        log.info('MT5 ready — starting gravity loop')
    else:
        log.warning('MT5 not ready at startup — gravity loop will retry on its own')

    # Start Level Gravity background updater (refreshes direction cache every 15s)
    tg = threading.Thread(target=_gravity_loop, name='gravity-updater', daemon=True)
    tg.start()
    log.info('Gravity updater thread started')

    # Start news awareness thread (polls calendar + headlines every 2min)
    if config.NEWS_FILTER_ENABLED:
        try:
            import news_filter as _nf
            _nf.start_awareness_thread(tg_alert_fn=_tg_alert)
        except Exception as _ne:
            log.debug(f'[News] Awareness thread start error (ignored): {_ne}')

    t = threading.Thread(target=run_engine, name='broker-engine', daemon=True)
    t.start()
    log.info('Broker engine thread started')
