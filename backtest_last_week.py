"""
backtest_last_week.py
─────────────────────
Simulates Autocycle AI Broker + Level Gravity filter on XAUUSD+ M1 data.

Default: last completed Mon–Fri week, 05:00–21:00 BST each day.
Override via CLI:
    python backtest_last_week.py                     # auto last week
    python backtest_last_week.py 2026-08-03          # specific week start
    python backtest_last_week.py 2026-08-03 2026-08-07

Flags:
    python backtest_last_week.py --no-gravity        # disable gravity filter

Settings match live broker exactly:
  • GUARDIAN_TOLERANCE = 0.00  (closes at exact SL level)
  • COOLDOWN_SECS = 15
  • Gravity: 5-vote HMA + trend gate + spike filter (replicated inline)
  • level_gravity.py is NOT modified — logic duplicated here for backtest only

Run on VPS with MT5 open:
    cd C:\\Users\\Administrator\\Documents\\autocycle
    python backtest_last_week.py
"""

import os
import sys
import math
from datetime import datetime, timezone, timedelta, date

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ── CLI args ──────────────────────────────────────────────────────────────────
_args = [a for a in sys.argv[1:] if not a.startswith('--')]
_flags = [a for a in sys.argv[1:] if a.startswith('--')]
GRAVITY_ENABLED  = '--no-gravity'   not in _flags
GUARDIAN_ENABLED = '--no-guardian'  not in _flags

# --guardian-buffer=0.50  → Guardian only fires if price reverses $0.50 PAST
# the SL level (toward entry). Default 0.00 = exact SL level (current live).
GUARDIAN_BUFFER  = 0.00

# --lot-size=0.001  → Scale all P&L by this lot size.
# 0.01 lots (default) = 1 oz Gold = P&L equals price move in dollars.
# 0.001 lots (nano)   = 0.1 oz   = P&L × 0.1  (for ~$10 accounts).
LOT_SIZE = 0.01

for _a in _flags:
    if _a.startswith('--guardian-buffer='):
        try: GUARDIAN_BUFFER = float(_a.split('=')[1])
        except: pass
    if _a.startswith('--lot-size='):
        try: LOT_SIZE = float(_a.split('=')[1])
        except: pass

_LOT_SCALE = LOT_SIZE / 0.01   # scale factor relative to 0.01 lot baseline

def _parse_dates():
    today = date.today()
    if len(_args) >= 2:
        ws = datetime.strptime(_args[0], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        we = datetime.strptime(_args[1], '%Y-%m-%d').replace(hour=23, minute=59, tzinfo=timezone.utc)
        return ws, we
    if len(_args) == 1:
        ws = datetime.strptime(_args[0], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        return ws, ws + timedelta(days=4, hours=23, minutes=59)
    days_since_mon = today.weekday()
    if days_since_mon == 0:
        days_since_mon = 7
    last_mon = today - timedelta(days=days_since_mon)
    last_fri = last_mon + timedelta(days=4)
    ws = datetime(last_mon.year, last_mon.month, last_mon.day, tzinfo=timezone.utc)
    we = datetime(last_fri.year, last_fri.month, last_fri.day, 23, 59, tzinfo=timezone.utc)
    return ws, we

WEEK_START, WEEK_END = _parse_dates()

# ── MT5 ───────────────────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    HAS_MT5 = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MT5_LOGIN    = int(os.getenv('MT5_LOGIN',    '0'))
MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')
MT5_SERVER   = os.getenv('MT5_SERVER',   '')
GOLD_SYMBOL  = os.getenv('MT5_SYMBOL',   'XAUUSD+')

# ── Broker config (mirrors autocycle_broker/config.py) ───────────────────────
ATR_PERIOD         = 14
ATR_MIN            = 1.20
SL_ATR_MULT        = 1.50
TP_EXTRA_MULT      = 0.25
SL_MIN             = 2.00
SL_MAX             = 5.00
TP_EXTRA_MIN       = 0.30
TP_EXTRA_MAX       = 1.00
GUARDIAN_TOLERANCE = 0.00       # exact SL price — only spread lost
# REVERSAL_TOLERANCE removed: same-bar $0.05 bounce fires on virtually every
# M1 Gold candle, making 99.7% of cycles breakeven before Vantage hedge opens.
# With gravity filtering, genuine SL fires go straight to survivor phase.
COMMISSION         = 0.03
SPREAD_COST        = 0.13       # approx Vantage spread on XAUUSD+ hedge
COOLDOWN_SECS      = 15

SESSION_START_UTC = 4   # 05:00 BST
SESSION_END_UTC   = 20  # 21:00 BST

# ── Gravity config (mirrors level_gravity.py — file NOT modified) ─────────────
LEVEL_INCREMENT  = 5.0    # $5 gravity levels
MIN_DIST_TO_TP   = 0.20   # price must be >$0.20 from nearest $5 level
HMA_FAST_P       = 5
HMA_SLOW_P       = 20
GRAVITY_CANDLES  = 50     # bars fed into gravity per entry check
ATR_PERIOD_G     = 14     # gravity ATR period (separate from broker ATR)
SPIKE_THRESHOLD  = 3.0    # $3 net move in 3 candles = news spike
SPIKE_BLOCK_BARS = 4      # block for ~4 minutes on M1


# ═════════════════════════════════════════════════════════════════════════════
# Gravity — replicated inline (level_gravity.py UNTOUCHED)
# ═════════════════════════════════════════════════════════════════════════════

def _wma(arr, p):
    """Weighted Moving Average over a numpy array."""
    w   = np.arange(1, p + 1, dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(p - 1, len(arr)):
        out[i] = np.dot(arr[i - p + 1 : i + 1], w) / w.sum()
    return out


def _hma(arr, p):
    """Hull Moving Average — near-zero lag, matches level_gravity._compute_hma."""
    half  = max(p // 2, 1)
    sqrtn = max(int(p ** 0.5), 1)
    return _wma(2 * _wma(arr, half) - _wma(arr, p), sqrtn)


def _nearest_levels_g(price):
    lb = math.floor(price / LEVEL_INCREMENT) * LEVEL_INCREMENT
    return round(lb, 2), round(lb + LEVEL_INCREMENT, 2)


def gravity_verdict(bars_slice, live):
    """
    Returns ('FIRE'|'SKIP', reason_str, bulls_score).
    bars_slice: list of bar dicts ending just before the entry bar (up to 50 bars).
    live: mid price at the entry bar.

    Replicates level_gravity.analyze_gravity_scalp():
      1. 5-vote smart direction (HMA cross, slope, level position,
         momentum velocity, candle exhaustion)
      2. Min dist-to-level check ($0.20 minimum)
    Trend gate and spike filter are tracked externally (see run_backtest).
    """
    if not HAS_NUMPY:
        return 'FIRE', 'numpy missing — gravity disabled', 5

    n = len(bars_slice)
    if n < 25:
        return 'SKIP', 'not enough history', 0

    closes = np.array([b['close'] for b in bars_slice], dtype=float)
    highs  = np.array([b['high']  for b in bars_slice], dtype=float)
    lows   = np.array([b['low']   for b in bars_slice], dtype=float)
    opens  = np.array([b['open']  for b in bars_slice], dtype=float)

    # ATR via EWM of H-L range
    hl    = highs - lows
    alpha = 2.0 / (ATR_PERIOD_G + 1)
    atr_g = float(hl[0])
    for v in hl[1:]:
        atr_g = alpha * float(v) + (1 - alpha) * atr_g
    if atr_g <= 0:
        return 'SKIP', 'ATR invalid', 0

    # HMA fast/slow + slope
    hf = _hma(closes, HMA_FAST_P)
    hs = _hma(closes, HMA_SLOW_P)

    # slope = diff over 3 bars (mirrors df['hma_fast'].diff(3))
    hma_slope = np.full(n, np.nan)
    for i in range(3, n):
        hma_slope[i] = hf[i] - hf[i - 3]

    hf_last = hf[-1];  hs_last = hs[-1]
    sl_last = hma_slope[-1]
    sl_prev = hma_slope[-2] if n >= 2 else np.nan

    if np.isnan(hf_last) or np.isnan(hs_last):
        return 'SKIP', 'HMA not ready', 0

    # Level position
    level_low, level_high = _nearest_levels_g(live)
    pos = (live - level_low) / LEVEL_INCREMENT   # 0=at lower level, 1=at upper

    # ── Vote 1: HMA5 cross ────────────────────────────────────────────────────
    v1 = bool(hf_last > hs_last)

    # ── Vote 2: HMA5 slope direction ─────────────────────────────────────────
    v2 = bool(sl_last > 0) if not np.isnan(sl_last) else v1

    # ── Vote 3: Level gravity position ───────────────────────────────────────
    if pos >= 0.65:
        v3 = False   # top 35% → near upper level → gravity says SELL
    elif pos <= 0.35:
        v3 = True    # bottom 35% → near lower level → gravity says BUY
    else:
        v3 = v1      # middle → follow HMA

    # ── Vote 4: Momentum velocity (reversal early warning) ───────────────────
    if not np.isnan(sl_last) and not np.isnan(sl_prev):
        accel = sl_last - sl_prev
        v4 = True if accel > 0 else (not v1)
    else:
        v4 = v1

    # ── Vote 5: Candle body exhaustion ───────────────────────────────────────
    if n >= 9:
        def body(k): return abs(float(closes[k]) - float(opens[k]))
        recent_avg = (body(-1) + body(-2) + body(-3)) / 3
        prior_avg  = (body(-4) + body(-5) + body(-6) + body(-7) + body(-8)) / 5
        v5 = (not v1) if (prior_avg > 0 and recent_avg < prior_avg * 0.60) else v1
    else:
        v5 = v1

    bulls = sum([v1, v2, v3, v4, v5])
    is_bull = bulls >= 3
    direction = 'BUY' if is_bull else 'SELL'

    # Min distance to TP level
    dist_to_level = (level_high - live) if is_bull else (live - level_low)
    if dist_to_level < MIN_DIST_TO_TP:
        return 'SKIP', f'too close to $5 level ({dist_to_level:.2f}<{MIN_DIST_TO_TP})', bulls

    return 'FIRE', f'dir={direction} score={bulls}/5', bulls


# ═════════════════════════════════════════════════════════════════════════════
# Broker helpers
# ═════════════════════════════════════════════════════════════════════════════

def calc_atr(highs, lows, closes, period=ATR_PERIOD):
    trs = []
    for i in range(len(closes)):
        trs.append(highs[i] - lows[i] if i == 0 else max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return [None if i < period else sum(trs[i - period + 1:i + 1]) / period
            for i in range(len(trs))]


def compute_sl_tp(atr_val):
    sl   = max(SL_MIN, min(SL_MAX,      round(atr_val * SL_ATR_MULT,   2)))
    tp   = max(TP_EXTRA_MIN, min(TP_EXTRA_MAX, round(atr_val * TP_EXTRA_MULT, 2)))
    return sl, tp


def in_session(dt_utc):
    return SESSION_START_UTC <= dt_utc.hour < SESSION_END_UTC


# ═════════════════════════════════════════════════════════════════════════════
# Data fetch
# ═════════════════════════════════════════════════════════════════════════════

def fetch_bars():
    if not HAS_MT5:
        print("ERROR: MetaTrader5 not found. Run: pip install MetaTrader5")
        sys.exit(1)

    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        ok = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
    else:
        ok = mt5.initialize()

    if not ok:
        print(f"ERROR: MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    mt5.symbol_select(GOLD_SYMBOL, True)
    from_dt = WEEK_START - timedelta(hours=3)   # extra buffer for gravity warmup
    to_dt   = WEEK_END   + timedelta(hours=2)

    rates = mt5.copy_rates_range(GOLD_SYMBOL, mt5.TIMEFRAME_M1, from_dt, to_dt)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        print(f"ERROR: No data returned for {GOLD_SYMBOL}")
        sys.exit(1)

    bars = [{'dt': datetime.fromtimestamp(r['time'], tz=timezone.utc),
             'open': float(r['open']), 'high': float(r['high']),
             'low': float(r['low']),   'close': float(r['close'])}
            for r in rates]

    print(f"Fetched {len(bars)} M1 bars  ({GOLD_SYMBOL}  {WEEK_START.date()} → {WEEK_END.date()})")
    return bars


# ═════════════════════════════════════════════════════════════════════════════
# Backtest engine
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(bars):
    highs  = [b['high']  for b in bars]
    lows   = [b['low']   for b in bars]
    closes = [b['close'] for b in bars]
    atrs   = calc_atr(highs, lows, closes)

    # Only session bars within the target week (with ATR ready)
    session_bars = [
        (i, bars[i], atrs[i])
        for i in range(len(bars))
        if in_session(bars[i]['dt'])
        and WEEK_START.date() <= bars[i]['dt'].date() <= WEEK_END.date()
        and atrs[i] is not None
    ]
    print(f"Session bars (in-hours + ATR ready): {len(session_bars)}")
    print()

    results = {
        'TP'                  : 0,
        'REVERSAL_AFTER_HEDGE': 0,   # Guardian fired (Vantage hedge closes at ~zero net)
        'TIMEOUT'             : 0,
        'GRAVITY_SKIP'        : 0,
    }
    cycle_details  = []
    cooldown_until = -1

    # ── Gravity state (tracked across bars) ───────────────────────────────────
    break_counter  = 0          # level break counter (-3 to +3)
    last_level_low = None       # previous $5 level low
    spike_blocked_until = -1    # bar index until spike block lifts

    i = 0
    while i < len(session_bars):
        idx, bar, atr_val = session_bars[i]

        # Cooldown
        if i <= cooldown_until:
            # Update gravity trend state even during cooldown
            live = bar['close']
            ll, _ = _nearest_levels_g(live)
            if last_level_low is not None:
                if ll < last_level_low - 0.1:   break_counter = max(break_counter - 1, -3)
                elif ll > last_level_low + 0.1: break_counter = min(break_counter + 1,  3)
            last_level_low = ll
            i += 1
            continue

        # ATR filter
        if atr_val < ATR_MIN:
            i += 1
            continue

        entry_mid = bar['close']
        entry_time = bar['dt'].strftime('%a %H:%M')

        # ── Update gravity trend state ─────────────────────────────────────────
        ll_now, lh_now = _nearest_levels_g(entry_mid)
        if last_level_low is not None:
            if ll_now < last_level_low - 0.1:
                break_counter = max(break_counter - 1, -3)
            elif ll_now > last_level_low + 0.1:
                break_counter = min(break_counter + 1, 3)
        last_level_low = ll_now

        # ── Spike filter ──────────────────────────────────────────────────────
        # Detect $3 net move in last 3 bars → block chasing direction for 4 bars
        if i >= 3:
            net_3 = bars[idx]['close'] - bars[max(0, idx - 3)]['close']
            if abs(net_3) > SPIKE_THRESHOLD:
                spike_blocked_until = i + SPIKE_BLOCK_BARS

        spike_active = (i <= spike_blocked_until)

        # ── Gravity check ─────────────────────────────────────────────────────
        if GRAVITY_ENABLED:
            # Build window of up to GRAVITY_CANDLES bars ending before entry bar
            window_start = max(0, idx - GRAVITY_CANDLES)
            bars_slice   = bars[window_start:idx]   # all bars, not just session bars

            verdict, reason, bulls = gravity_verdict(bars_slice, entry_mid)

            # Trend gate: 2+ consecutive same-direction level breaks = TREND mode
            trend_mode = 'NORMAL'
            if break_counter <= -2: trend_mode = 'TREND_DOWN'
            elif break_counter >= 2: trend_mode = 'TREND_UP'

            if trend_mode == 'TREND_DOWN' and bulls >= 3:
                # 5-vote says BUY but trend is DOWN → SKIP (counter-trend)
                verdict = 'SKIP'
                reason  = f'TREND DOWN — BUY blocked (bc={break_counter})'
            elif trend_mode == 'TREND_UP' and bulls < 3:
                # 5-vote says SELL but trend is UP → SKIP
                verdict = 'SKIP'
                reason  = f'TREND UP — SELL blocked (bc={break_counter})'

            if spike_active and verdict == 'FIRE':
                verdict = 'SKIP'
                reason  = f'spike filter active ({spike_blocked_until - i} bars left)'

            if verdict == 'SKIP':
                results['GRAVITY_SKIP'] += 1
                cycle_details.append({
                    'time'    : entry_time,
                    'entry'   : entry_mid,
                    'sl_dist' : 0,
                    'tp_extra': 0,
                    'sl_fired': '—',
                    'outcome' : 'GRAVITY_SKIP',
                    'int_pnl' : 0,
                    'van_pnl' : 0,
                    'grav_reason': reason,
                })
                i += 1
                continue
        else:
            bulls = 5

        # ── Open cycle ─────────────────────────────────────────────────────────
        sl_dist, tp_extra = compute_sl_tp(atr_val)
        buy_sl   = round(entry_mid - sl_dist, 2)
        sell_sl  = round(entry_mid + sl_dist, 2)

        # ── Wait for SL fire ───────────────────────────────────────────────────
        sl_fired = None; sl_fire_bar_i = None; sl_price = None

        for j in range(i + 1, len(session_bars)):
            _, jbar, _ = session_bars[j]
            if not in_session(jbar['dt']): break
            hit_buy  = jbar['low']  <= buy_sl
            hit_sell = jbar['high'] >= sell_sl
            if hit_buy or hit_sell:
                if hit_buy and hit_sell:
                    sl_fired = 'BUY' if abs(jbar['open'] - buy_sl) <= abs(jbar['open'] - sell_sl) else 'SELL'
                else:
                    sl_fired = 'BUY' if hit_buy else 'SELL'
                sl_price      = buy_sl if sl_fired == 'BUY' else sell_sl
                sl_fire_bar_i = j
                break

        if sl_fired is None:
            results['TIMEOUT'] += 1
            i += 1
            continue

        # ── Vantage hedge opens immediately on SL fire → survivor phase ──────────
        # (Same-bar reversal check removed — on M1 Gold a $0.05 bounce fires
        #  on virtually every candle, preventing the hedge from ever opening.)
        # GUARDIAN_BUFFER: how far price can reverse past the SL toward entry
        # before Guardian fires. 0.00 = exact SL level. 0.50 = SL + $0.50 room.
        if sl_fired == 'BUY':
            tp_target = round(sl_price - tp_extra, 2)
            guardian  = round(sl_price + GUARDIAN_BUFFER, 2)  # price must bounce $buffer above SL
        else:
            tp_target = round(sl_price + tp_extra, 2)
            guardian  = round(sl_price - GUARDIAN_BUFFER, 2)  # price must bounce $buffer below SL

        outcome = None; outcome_bar_i = None

        for k in range(sl_fire_bar_i + 1, len(session_bars)):
            _, kbar, _ = session_bars[k]
            if not in_session(kbar['dt']): break
            if sl_fired == 'BUY':
                tp_hit       = kbar['low']  <= tp_target
                guardian_hit = GUARDIAN_ENABLED and kbar['high'] >= guardian
            else:
                tp_hit       = kbar['high'] >= tp_target
                guardian_hit = GUARDIAN_ENABLED and kbar['low']  <= guardian
            if tp_hit or guardian_hit:
                outcome       = 'TP' if tp_hit else 'REVERSAL_AFTER_HEDGE'
                outcome_bar_i = k
                break

        if outcome is None:
            # SL fired but session ended before TP or Guardian resolved.
            # With Guardian ON  → Guardian would eventually fire → count as Guardian.
            # With Guardian OFF → position stays open overnight → count as TIMEOUT.
            outcome       = 'REVERSAL_AFTER_HEDGE' if GUARDIAN_ENABLED else 'TIMEOUT'
            outcome_bar_i = sl_fire_bar_i

        # ── P&L ───────────────────────────────────────────────────────────────
        if outcome == 'TP':
            int_pnl = round((tp_extra - COMMISSION) * _LOT_SCALE, 4)
            van_pnl = round(tp_extra * _LOT_SCALE, 4)
        elif outcome == 'TIMEOUT':
            # SL fired but session ended. Internal paid commission, Vantage
            # position open overnight — outcome unknown, model as 0 (neutral).
            int_pnl = round(-COMMISSION * _LOT_SCALE, 4)
            van_pnl = 0.0
        else:
            # REVERSAL_AFTER_HEDGE — Guardian: survivor closes at SL + GUARDIAN_BUFFER.
            # Vantage net = -GUARDIAN_BUFFER (gave back that much profit) - spread.
            int_pnl = round(-COMMISSION * _LOT_SCALE, 4)
            van_pnl = round(-(SPREAD_COST + GUARDIAN_BUFFER) * _LOT_SCALE, 4)

        results[outcome] += 1
        cycle_details.append({
            'time': entry_time, 'entry': entry_mid, 'sl_dist': sl_dist,
            'tp_extra': tp_extra, 'sl_fired': sl_fired, 'outcome': outcome,
            'int_pnl': int_pnl, 'van_pnl': van_pnl, 'grav_reason': '',
        })
        cooldown_until = outcome_bar_i or sl_fire_bar_i
        i = cooldown_until + 1

    return results, cycle_details


# ═════════════════════════════════════════════════════════════════════════════
# Report
# ═════════════════════════════════════════════════════════════════════════════

def print_report(results, details):
    traded  = [d for d in details if d['outcome'] != 'GRAVITY_SKIP']
    total   = len(traded)
    tp      = results['TP']
    guard   = results['REVERSAL_AFTER_HEDGE']
    tout    = results['TIMEOUT']
    gskip   = results['GRAVITY_SKIP']

    int_total = sum(d['int_pnl'] for d in traded)
    van_total = sum(d['van_pnl'] for d in traded)
    net_total = int_total + van_total

    gmode  = 'ON' if GRAVITY_ENABLED  else 'OFF (--no-gravity)'
    if not GUARDIAN_ENABLED:
        gumode = 'OFF (--no-guardian)'
    elif GUARDIAN_BUFFER > 0:
        gumode = f'ON  buffer=${GUARDIAN_BUFFER:.2f} (fires ${GUARDIAN_BUFFER:.2f} past SL)'
    else:
        gumode = 'ON  buffer=$0.00 (exact SL level)'

    print()
    print("═" * 64)
    print("  AUTOCYCLE AI BROKER — BACKTEST RESULTS")
    print(f"  {GOLD_SYMBOL}  M1  ·  {WEEK_START.date()} → {WEEK_END.date()}  ·  05:00–21:00 BST")
    print(f"  Cooldown: {COOLDOWN_SECS}s  |  Lot size: {LOT_SIZE} lots  |  Gravity: {gmode}")
    print(f"  Guardian: {gumode}")
    print("═" * 64)
    print(f"  Gravity-blocked cycles : {gskip}")
    print(f"  Cycles opened          : {total}")
    if total:
        print(f"  TP hits                : {tp:>4}  ({tp/total*100:.1f}%)")
        print(f"  Guardian fires         : {guard:>4}  ({guard/total*100:.1f}%)")
        print(f"  Session timeouts       : {tout:>4}  ({tout/total*100:.1f}%)")
        print()
        print(f"  TP rate                : {tp/total*100:.1f}%")
        tp_needed = round(SPREAD_COST / (SPREAD_COST + (sum(d['van_pnl'] for d in traded if d['outcome']=='TP') / tp if tp else 0.5)) * 100, 1) if tp else '?'
        guardcost = (COMMISSION + SPREAD_COST)
        tpearn    = sum(d['int_pnl']+d['van_pnl'] for d in traded if d['outcome']=='TP') / tp if tp else 0
        breakeven_rate = guardcost / (guardcost + tpearn) * 100 if tpearn > 0 else '?'
        print(f"  Breakeven TP rate      : {breakeven_rate:.1f}%  (need this % TP to net zero)")
    print()
    print(f"  Internal balance P&L   : ${int_total:+.2f}")
    print(f"  Vantage hedge P&L      : ${van_total:+.2f}")
    print(f"  Combined net P&L       : ${net_total:+.2f}")
    if total:
        print(f"  P&L per cycle          : ${net_total/total:+.4f}")
    if tp > 0:
        avg = sum(d['int_pnl']+d['van_pnl'] for d in traded if d['outcome']=='TP') / tp
        print(f"  Avg earn per TP        : ${avg:+.2f}")
    if guard > 0:
        avg = sum(d['int_pnl']+d['van_pnl'] for d in traded if d['outcome']=='REVERSAL_AFTER_HEDGE') / guard
        print(f"  Avg cost per Guardian  : ${avg:+.2f}")
    print("═" * 64)

    print()
    print("  CYCLE LOG (first 40 traded cycles)")
    print("  " + "─" * 64)
    print(f"  {'Time':>8}  {'Entry':>8}  {'SL':>5}  {'TP+':>4}  "
          f"{'Side':>4}  {'Outcome':<10}  {'Int':>6}  {'Van':>6}")
    print("  " + "─" * 64)
    shown = 0
    for d in details:
        if d['outcome'] == 'GRAVITY_SKIP':
            continue
        out_short = d['outcome'].replace('REVERSAL_AFTER_HEDGE', 'GUARDIAN').replace('TIMEOUT', 'TIMEOUT')
        print(
            f"  {d['time']:>8}  {d['entry']:>8.2f}"
            f"  {d['sl_dist']:>5.2f}  {d['tp_extra']:>4.2f}"
            f"  {d['sl_fired']:>4}  {out_short:<10}"
            f"  {d['int_pnl']:>+6.2f}  {d['van_pnl']:>+6.2f}"
        )
        shown += 1
        if shown >= 40:
            remaining = sum(1 for x in details[details.index(d)+1:] if x['outcome'] != 'GRAVITY_SKIP')
            if remaining: print(f"  ... and {remaining} more traded cycles")
            break
    print("═" * 64)
    print()


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    if GRAVITY_ENABLED and not HAS_NUMPY:
        print("WARNING: numpy not found — gravity filter disabled.")
        print("Install: pip install numpy --break-system-packages")
        print()

    print("Autocycle AI Broker — Backtest")
    print(f"Week   : {WEEK_START.date()} → {WEEK_END.date()}")
    print(f"Symbol : {GOLD_SYMBOL}  |  Session: 05:00–21:00 BST")
    print(f"Gravity:  {'ON (5-vote + trend gate + spike filter)' if GRAVITY_ENABLED else 'OFF (--no-gravity)'}")
    print(f"Guardian: {'ON (survivor closes at SL level)'       if GUARDIAN_ENABLED else 'OFF — survivor runs to TP or timeout (--no-guardian)'}")
    print()
    print("Fetching M1 data from MT5…")
    bars = fetch_bars()

    session_count = sum(
        1 for b in bars
        if in_session(b['dt'])
        and WEEK_START.date() <= b['dt'].date() <= WEEK_END.date()
    )
    print(f"In-session bars (05:00–21:00 BST, Mon–Fri): {session_count}")

    results, details = run_backtest(bars)
    print_report(results, details)
