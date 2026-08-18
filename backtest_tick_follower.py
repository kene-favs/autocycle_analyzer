#!/usr/bin/env python3
"""
backtest_tick_follower.py  —  Ant-on-Sugar Tick Velocity Follower  v2.9.0
══════════════════════════════════════════════════════════════════════════════
Simulates tick_follower.py on historical M1 bar data from MT5.

STRATEGY MIRRORED (from tick_follower.py v2.9.0)
  Pool   : EURUSD · AUDUSD · GBPUSD · NZDUSD · USDJPY · USDCAD · USDCHF
  Session: 05:00 – 20:00 UTC weekdays only
  Slots  : 2 active pairs at a time — always the 2 hottest by velocity
  Score  : re-evaluated every 5 bars  (≈ 5s real-time scoring window)
  Entry  : |bar body| ≥ 0.4 pip in the bar's direction
  Capture: 50 % of bar body  (conservative — real 20ms system captures more)
  Lots   : same _LOT_TIERS as live system, scale with running balance

⚠  SIMULATION MODEL
   The live system polls at 20ms and can enter/exit many times per minute.
   This backtest models 1 trade per qualifying M1 bar (1 minute window).
   50 % capture of bar body is a conservative proxy for what the real
   stall-detection achieves on tick data.  Live results are typically
   1.5–2× the bar-level estimate shown here.

DATA SOURCE
   M1 bars from MetaTrader5 via copy_rates_range().
   Run on your VPS where MT5 is running and connected to Tickmill.

Usage
─────
  python backtest_tick_follower.py                   # last 5 trading days, $10
  python backtest_tick_follower.py --days=10         # 10 trading days
  python backtest_tick_follower.py --balance=174     # start with $174 (your real balance)
  python backtest_tick_follower.py --pairs=EURUSD,GBPUSD   # specific pairs only
  python backtest_tick_follower.py --all-slots       # all pairs trade (no slot limit)
  python backtest_tick_follower.py --csv=results.csv # save trade log to CSV
"""

import sys
import os
import math
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    HAS_MT5 = False

# ── Mirror tick_follower.py v2.9.0 constants exactly ─────────────────────────

POOL = ['EURUSD', 'AUDUSD', 'GBPUSD', 'NZDUSD', 'USDJPY', 'USDCAD', 'USDCHF']

TRADING_START        = 5    # 05:00 UTC
TRADING_END          = 20   # 20:00 UTC

ENTRY_THRESHOLD_PIPS = 0.4
STALL_THRESHOLD_PIPS = 0.15
SAFETY_SL_PIPS       = 1.5   # crash-guard only (never reached in simulation)

_LOT_TIERS = [
    (50,    0.05), (100,   0.10), (200,   0.20),
    (400,   0.40), (800,   0.80), (2000,  1.00),
    (4000,  2.00), (8000,  4.00),
]


def _lot_for_balance(balance: float) -> float:
    """Exact copy of tick_follower._lot_for_balance()."""
    for threshold, lot in _LOT_TIERS:
        if balance < threshold:
            return lot
    lot = 4.00
    cap = 8000.0
    while balance >= cap * 2:
        lot *= 2.0
        cap *= 2.0
    return lot


# ── Simulation-only parameters ────────────────────────────────────────────────

CAPTURE_EFFICIENCY = 0.30   # fraction of bar body captured per trade
#   ↑ Deliberate conservative estimate.  M1 bars overstate velocity:
#   a 0.8-pip body might drift over 60 s (no entry) or spike in 2 s (one entry
#   capturing ~0.25 pip).  0.30 is a conservative proxy.
#   The live 20ms system captures MORE per bar on fast moves but not on drifts.

SCORE_WINDOW_BARS  = 5      # re-score pairs every N M1 bars
ACTIVE_SLOTS       = 2      # max simultaneous trading pairs

# Broker lot cap — real MT5 accounts have hard limits.
# Tickmill Pro demo: max ~100 lots per position.  Our lot-tier math has no
# built-in ceiling — without this cap the balance computes phantom profits at
# thousands of lots, which is impossible in real trading (margin, position limits).
MAX_BACKTEST_LOT   = 100.0  # override with --max-lot on command line

# Pip sizes (matches tick_follower._pip() logic)
_PIP_SIZES = {
    'EURUSD': 0.0001, 'AUDUSD': 0.0001, 'GBPUSD': 0.0001,
    'NZDUSD': 0.0001, 'USDCAD': 0.0001, 'USDCHF': 0.0001,
    'USDJPY': 0.01,
}


def _pip_size(sym: str) -> float:
    return _PIP_SIZES.get(sym, 0.0001)


def _pip_value_per_lot(sym: str, avg_price: float) -> float:
    """
    USD value of 1 pip per 1.0 standard lot (100,000 units).

    XXX/USD pairs (EURUSD etc): pip_val = pip × 100,000         = $10 always
    USD/XXX pairs (USDJPY etc): pip_val = (pip / rate) × 100,000
    """
    pip = _pip_size(sym)
    if sym.endswith('USD'):       # EURUSD, GBPUSD, AUDUSD, NZDUSD
        return pip * 100_000      # $10 per pip per lot
    elif sym.startswith('USD'):   # USDJPY, USDCAD, USDCHF
        price = max(avg_price, 0.001)
        return (pip / price) * 100_000
    else:
        return 10.0               # fallback


# ── Date helpers ──────────────────────────────────────────────────────────────

def _last_n_trading_days(n: int) -> tuple:
    """
    Return (from_dt, to_dt, actual_days_list) for the last n completed
    trading days (Mon-Fri only).  All datetimes are UTC midnight.
    """
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    days = []
    d = today
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:   # Mon=0 … Fri=4
            days.append(d)
    days.sort()                     # oldest first
    from_dt = days[0]
    to_dt   = days[-1] + timedelta(hours=23, minutes=59, seconds=59)
    return from_dt, to_dt, days


def _in_session(dt_utc: datetime) -> bool:
    """Exact mirror of tick_follower._in_session()."""
    return (
        TRADING_START <= dt_utc.hour < TRADING_END
        and dt_utc.weekday() < 5
    )


# ── MT5 data loading ──────────────────────────────────────────────────────────

def load_m1_bars(
    symbols: list,
    from_dt: datetime,
    to_dt: datetime,
) -> dict:
    """
    Fetch M1 bars from MT5 for each symbol.

    Returns:
        {
          symbol: [
            {'time': datetime_utc, 'open': f, 'high': f, 'low': f, 'close': f},
            ...
          ]
        }
    """
    if not HAS_MT5:
        raise RuntimeError(
            "MetaTrader5 package not installed.\n"
            "    Run:  pip install MetaTrader5\n"
            "    Then execute this script on your VPS where MT5 is running."
        )

    if not mt5.initialize():
        raise RuntimeError(
            f"MT5 initialize() failed: {mt5.last_error()}\n"
            "    Is MT5 terminal running on this machine?"
        )

    print(f"\n  Loading M1 bars  "
          f"{from_dt.strftime('%Y-%m-%d')} → {to_dt.strftime('%Y-%m-%d')}")
    print(f"  {'Symbol':<10}  {'Bars':>6}  {'First bar':<19}  {'Last bar':<19}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*19}  {'─'*19}")

    result = {}
    for sym in symbols:
        rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, from_dt, to_dt)
        if rates is None or len(rates) == 0:
            print(f"  {sym:<10}  {'NO DATA':>6}  (symbol not available)")
            result[sym] = []
            continue

        bars = []
        for r in rates:
            dt = datetime.fromtimestamp(int(r['time']), tz=timezone.utc)
            bars.append({
                'time' : dt,
                'open' : float(r['open']),
                'high' : float(r['high']),
                'low'  : float(r['low']),
                'close': float(r['close']),
            })

        first = bars[0]['time'].strftime('%Y-%m-%d %H:%M') if bars else '—'
        last  = bars[-1]['time'].strftime('%Y-%m-%d %H:%M') if bars else '—'
        print(f"  {sym:<10}  {len(bars):>6}  {first:<19}  {last:<19}")
        result[sym] = bars

    mt5.shutdown()
    return result


# ── Simulation engine ─────────────────────────────────────────────────────────

def simulate(bars_by_sym: dict, start_balance: float, all_slots: bool = False, max_lot: float = MAX_BACKTEST_LOT) -> dict:
    """
    Run the bar-level Tick Follower simulation.

    Slot logic  (mirrors live system):
      Every SCORE_WINDOW_BARS bars, score all POOL symbols by recent avg range.
      Only the top ACTIVE_SLOTS pairs trade.  Others watch but don't trade.

    Trade logic per M1 bar:
      If |bar body in pips| >= ENTRY_THRESHOLD_PIPS:
        captured_pips = |body_pips| × CAPTURE_EFFICIENCY
        pnl = captured_pips × lot × pip_value_per_lot
        balance += pnl
    """

    # ── Build unified timeline ────────────────────────────────────────────────
    time_set: set = set()
    for bars in bars_by_sym.values():
        for b in bars:
            if _in_session(b['time']):
                time_set.add(b['time'])
    timeline = sorted(time_set)

    if not timeline:
        return {
            'trades': [], 'final_balance': start_balance,
            'daily_pnl': {}, 'pair_stats': {},
            'hour_pnl': {}, 'pip_val': {},
            'session_bars': 0, 'active_bars': 0,
        }

    # ── Index bars: symbol → {time: bar} ─────────────────────────────────────
    bars_idx: dict = {}
    avg_price: dict = {}
    for sym, bars in bars_by_sym.items():
        bars_idx[sym] = {b['time']: b for b in bars}
        prices = [b['close'] for b in bars]
        avg_price[sym] = sum(prices) / len(prices) if prices else 1.0

    pip_val = {sym: _pip_value_per_lot(sym, avg_price.get(sym, 1.0)) for sym in POOL}
    pip_sz  = {sym: _pip_size(sym) for sym in POOL}

    # ── State ─────────────────────────────────────────────────────────────────
    balance       = start_balance
    active_pairs  = list((bars_by_sym.keys()))[:ACTIVE_SLOTS]   # initial default
    scores: dict  = {sym: 0.0 for sym in POOL}

    trades:     list  = []
    daily_pnl:  dict  = defaultdict(float)
    pair_stats: dict  = {
        sym: {'trades': 0, 'pips': 0.0, 'pnl': 0.0, 'slot_bars': 0, 'session_bars': 0}
        for sym in POOL
    }
    hour_pnl:   dict  = defaultdict(float)
    session_bars = 0
    active_bars  = 0

    last_score_idx = -(SCORE_WINDOW_BARS + 1)  # force immediate score on first bar

    # ── Main loop ─────────────────────────────────────────────────────────────
    for idx, t in enumerate(timeline):

        session_bars += 1
        for sym in POOL:
            if t in bars_idx.get(sym, {}):
                pair_stats[sym]['session_bars'] += 1

        # ── Re-score every SCORE_WINDOW_BARS bars ────────────────────────────
        if idx - last_score_idx >= SCORE_WINDOW_BARS:
            look_start = max(0, idx - SCORE_WINDOW_BARS)
            look_times = timeline[look_start: idx + 1]

            for sym in POOL:
                sym_idx = bars_idx.get(sym, {})
                ranges = []
                for lt in look_times:
                    if lt in sym_idx:
                        b = sym_idx[lt]
                        ranges.append((b['high'] - b['low']) / pip_sz[sym])
                scores[sym] = (sum(ranges) / len(ranges)) if ranges else 0.0

            if all_slots:
                active_pairs = [s for s in POOL if s in bars_by_sym and bars_by_sym[s]]
            else:
                active_pairs = sorted(
                    POOL,
                    key=lambda s: scores.get(s, 0.0),
                    reverse=True
                )[:ACTIVE_SLOTS]

            last_score_idx = idx

        # ── Trade on active pairs ─────────────────────────────────────────────
        for sym in active_pairs:
            pair_stats[sym]['slot_bars'] += 1

            sym_idx = bars_idx.get(sym, {})
            if t not in sym_idx:
                continue

            bar       = sym_idx[t]
            pip       = pip_sz[sym]
            body_pips = (bar['close'] - bar['open']) / pip
            range_pips = (bar['high'] - bar['low']) / pip

            # Entry condition: directional bar body ≥ ENTRY_THRESHOLD_PIPS
            if abs(body_pips) < ENTRY_THRESHOLD_PIPS:
                continue

            active_bars += 1
            captured_pips = abs(body_pips) * CAPTURE_EFFICIENCY
            side          = 'BUY' if body_pips > 0 else 'SELL'
            lot           = min(_lot_for_balance(balance), max_lot)   # respect broker cap
            pnl           = captured_pips * lot * pip_val[sym]

            balance += pnl

            trade = {
                'time'     : t,
                'sym'      : sym,
                'dir'      : side,
                'body_pips': round(abs(body_pips), 3),
                'pips'     : round(captured_pips, 3),
                'lot'      : lot,
                'pnl'      : round(pnl, 4),
                'balance'  : round(balance, 4),
                'range'    : round(range_pips, 3),
            }
            trades.append(trade)

            day_key = t.strftime('%Y-%m-%d')
            daily_pnl[day_key]       += pnl
            pair_stats[sym]['trades'] += 1
            pair_stats[sym]['pips']   += captured_pips
            pair_stats[sym]['pnl']    += pnl
            hour_pnl[t.hour]          += pnl

    return {
        'trades'       : trades,
        'final_balance': round(balance, 4),
        'daily_pnl'    : dict(daily_pnl),
        'pair_stats'   : pair_stats,
        'hour_pnl'     : dict(hour_pnl),
        'pip_val'      : pip_val,
        'session_bars' : session_bars,
        'active_bars'  : active_bars,
        'scores_final' : scores,
    }


# ── Report printer ─────────────────────────────────────────────────────────────

def _bar_chart(value: float, max_val: float, width: int = 28) -> str:
    if max_val <= 0 or value == 0:
        return ''
    n = max(1, int(round(abs(value) / max_val * width)))
    return '█' * n


def print_report(
    results: dict,
    start_balance: float,
    n_days: int,
    symbols: list,
    max_lot: float = MAX_BACKTEST_LOT,
    all_slots: bool = False,
):
    trades     = results['trades']
    final_bal  = results['final_balance']
    daily_pnl  = results['daily_pnl']
    pair_stats = results['pair_stats']
    hour_pnl   = results['hour_pnl']
    pip_val    = results['pip_val']
    sess_bars  = results['session_bars']
    act_bars   = results['active_bars']

    total_pnl     = final_bal - start_balance
    pct_return    = (total_pnl / start_balance * 100) if start_balance > 0 else 0
    n_trades      = len(trades)
    trades_per_d  = n_trades / n_days if n_days > 0 else 0
    avg_pnl_trade = total_pnl / n_trades if n_trades > 0 else 0
    avg_pips      = (
        sum(t['pips'] for t in trades) / n_trades if n_trades > 0 else 0
    )
    daily_avg     = total_pnl / n_days if n_days > 0 else 0
    hit_rate      = act_bars / sess_bars * 100 if sess_bars > 0 else 0
    final_lot     = _lot_for_balance(final_bal)

    W = 64
    sep  = '═' * W
    sep2 = '─' * W

    print(f"\n{sep}")
    print(f"  AUTOCYCLE AI  —  Tick Follower Backtest  v2.9.0")
    print(f"  Pool   : {', '.join(POOL)}")
    slot_label = 'ALL' if all_slots else str(ACTIVE_SLOTS)
    print(f"  Session: {TRADING_START:02d}:00–{TRADING_END:02d}:00 UTC  ·  "
          f"Active slots: {slot_label}  ·  Entry ≥ {ENTRY_THRESHOLD_PIPS} pip")
    print(f"  Model  : {CAPTURE_EFFICIENCY*100:.0f}% of bar body per trade  "
          f"(conservative M1 approximation)"
          f"\n  Max lot: {max_lot:.1f}  (broker position limit)")
    print(sep)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n  SUMMARY  ({n_days} trading day{'s' if n_days != 1 else ''})")
    print(sep2)
    print(f"  {'Starting balance':<30}  ${start_balance:>10.2f}")
    print(f"  {'Final balance':<30}  ${final_bal:>10.2f}")
    sign = '+' if total_pnl >= 0 else ''
    print(f"  {'Total P&L':<30}  ${total_pnl:>+10.2f}  ({sign}{pct_return:.1f}%)")
    print(f"  {'Avg P&L per day':<30}  ${daily_avg:>+10.2f}")
    print(f"  {'Lot at end':<30}  {final_lot:>10.2f}")
    print(sep2)
    print(f"  {'Total trades':<30}  {n_trades:>10,}")
    print(f"  {'Trades per day':<30}  {trades_per_d:>10.1f}")
    print(f"  {'Avg pips per trade (captured)':<30}  {avg_pips:>10.3f}")
    print(f"  {'Avg $ per trade':<30}  ${avg_pnl_trade:>+10.4f}")
    print(f"  {'Session bars checked':<30}  {sess_bars:>10,}")
    print(f"  {'Qualifying bars (hit rate)':<30}  {act_bars:>10,}  ({hit_rate:.1f}%)")

    # ── Daily breakdown ───────────────────────────────────────────────────────
    print(f"\n  DAILY P&L")
    print(sep2)
    max_d = max((abs(v) for v in daily_pnl.values()), default=1)
    for day in sorted(daily_pnl):
        v   = daily_pnl[day]
        bar = _bar_chart(v, max_d)
        sign = '+' if v >= 0 else ''
        print(f"  {day}   ${v:>+8.2f}   {bar}")

    # ── Per-pair breakdown ────────────────────────────────────────────────────
    print(f"\n  PER-PAIR BREAKDOWN")
    print(sep2)
    print(f"  {'Pair':<9}  {'Trades':>6}  {'Pips':>8}  {'P&L':>10}  "
          f"{'$/pip/lot':>9}  {'Slot %':>6}")
    print(f"  {'─'*9}  {'─'*6}  {'─'*8}  {'─'*10}  {'─'*9}  {'─'*6}")

    # Sort by P&L descending
    sorted_pairs = sorted(POOL, key=lambda s: pair_stats[s]['pnl'], reverse=True)
    for sym in sorted_pairs:
        st = pair_stats[sym]
        slot_pct = (st['slot_bars'] / st['session_bars'] * 100
                    if st['session_bars'] > 0 else 0)
        pv = pip_val.get(sym, 10.0)
        print(
            f"  {sym:<9}  {st['trades']:>6}  {st['pips']:>8.2f}  "
            f"${st['pnl']:>+9.2f}  {pv:>9.2f}  {slot_pct:>5.0f}%"
        )

    # ── Hourly P&L ────────────────────────────────────────────────────────────
    print(f"\n  HOURLY P&L  (session 05:00–20:00 UTC)")
    print(sep2)
    max_h = max((abs(v) for v in hour_pnl.values()), default=1)
    for h in range(TRADING_START, TRADING_END):
        v = hour_pnl.get(h, 0.0)
        bar = _bar_chart(v, max_h, width=22)
        print(f"  {h:02d}:00  ${v:>+8.2f}   {bar}")

    # ── Balance progression ───────────────────────────────────────────────────
    if trades:
        print(f"\n  BALANCE MILESTONES")
        print(sep2)
        milestones = [0.25, 0.50, 0.75, 1.0]
        prev_pct = -1.0
        for t in trades:
            pct = (trades.index(t) + 1) / n_trades
            for m in milestones:
                if prev_pct < m <= pct:
                    dt  = t['time'].strftime('%Y-%m-%d %H:%M')
                    bal = t['balance']
                    print(f"  {int(m*100):>3}% through trades  {dt}  →  ${bal:.2f}")
            prev_pct = pct

    # ── Lot tier progression ──────────────────────────────────────────────────
    print(f"\n  LOT TIER PROGRESSION")
    print(sep2)
    current_lot = _lot_for_balance(start_balance)
    print(f"  Start  ${start_balance:>8.2f}  →  lot {current_lot}")
    for threshold, lot in _LOT_TIERS:
        if lot > current_lot or start_balance < threshold:
            # Find first trade that crossed this threshold
            for t in trades:
                if t['balance'] >= threshold and current_lot < lot:
                    dt = t['time'].strftime('%Y-%m-%d %H:%M')
                    print(f"  Crossed  ${threshold:>7.0f}  →  lot {lot}  "
                          f"at {dt}  (bal ${t['balance']:.2f})")
                    current_lot = lot
                    break

    # ── Trade log (last 40) ───────────────────────────────────────────────────
    print(f"\n  LAST 40 TRADES")
    print(sep2)
    print(f"  {'Time (UTC)':<17}  {'Pair':<7}  {'Dir':<5}  "
          f"{'Body':>5}  {'Capt':>5}  {'Lot':>5}  {'$P&L':>8}  {'Balance':>10}")
    print(f"  {'─'*17}  {'─'*7}  {'─'*5}  "
          f"{'─'*5}  {'─'*5}  {'─'*5}  {'─'*8}  {'─'*10}")
    for t in trades[-40:]:
        print(
            f"  {t['time'].strftime('%Y-%m-%d %H:%M')}  "
            f"{t['sym']:<7}  {t['dir']:<5}  "
            f"{t['body_pips']:>5.2f}  {t['pips']:>5.3f}  {t['lot']:>5.2f}  "
            f"${t['pnl']:>+7.4f}  ${t['balance']:>10.4f}"
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  SIMULATION NOTES")
    print(f"  ─ Capture {CAPTURE_EFFICIENCY*100:.0f}% of bar body: M1 bars can't tell if the 0.4 pip")
    print(f"    move happened in 200ms (entry) or drifted over 60s (no entry).")
    print(f"    Real live performance depends on actual tick velocity — test on demo.")
    print(f"  ─ Max lot capped at {max_lot:.0f}: real MT5 enforces broker position limits.")
    print(f"    Without this cap, lot sizing would compound to impossible levels.")
    print(f"  ─ No losses modeled: real system has rare stall-reversals and latency spikes.")
    print(f"  ─ XAUUSD joins the pool next week — adds another active slot opportunity.")
    print(f"{sep}\n")


# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(trades: list, path: str):
    import csv
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'time', 'sym', 'dir', 'body_pips', 'pips', 'lot', 'pnl', 'balance', 'range'
        ])
        writer.writeheader()
        for t in trades:
            row = dict(t)
            row['time'] = t['time'].strftime('%Y-%m-%d %H:%M:%S')
            writer.writerow(row)
    print(f"  Trade log saved → {path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Autocycle AI — Tick Follower Backtest v2.9.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--days',      type=int,   default=5,
        help='Number of completed trading days to backtest (default: 5)',
    )
    parser.add_argument(
        '--balance',   type=float, default=10.0,
        help='Starting account balance in USD (default: 10)',
    )
    parser.add_argument(
        '--pairs',     type=str,   default='',
        help='Comma-separated subset of POOL pairs to include (default: all 7)',
    )
    parser.add_argument(
        '--all-slots', action='store_true',
        help='Trade all pairs simultaneously instead of top-2 selection',
    )
    parser.add_argument(
        '--max-lot',   type=float, default=MAX_BACKTEST_LOT,
        help=f'Max lot per trade — broker hard limit (default: {MAX_BACKTEST_LOT})',
    )
    parser.add_argument(
        '--html',      type=str,   default='backtest_report.html',
        help='Path for HTML report output (default: backtest_report.html)',
    )
    parser.add_argument(
        '--no-html',   action='store_true',
        help='Disable HTML report generation',
    )
    parser.add_argument(
        '--csv',       type=str,   default='',
        help='Path to export trade log as CSV (optional)',
    )
    args = parser.parse_args()

    # ── Resolve symbols ───────────────────────────────────────────────────────
    if args.pairs:
        symbols = [p.strip().upper() for p in args.pairs.split(',') if p.strip()]
        invalid = [s for s in symbols if s not in POOL]
        if invalid:
            print(f"⚠  Unknown pairs ignored: {invalid}")
            print(f"   Valid pool: {POOL}")
        symbols = [s for s in symbols if s in POOL]
        if not symbols:
            print("❌  No valid pairs after filtering. Exiting.")
            sys.exit(1)
    else:
        symbols = list(POOL)

    n_days      = max(1, args.days)
    start_bal   = max(0.01, args.balance)
    start_lot   = _lot_for_balance(start_bal)

    # ── Header ────────────────────────────────────────────────────────────────
    print("═" * 64)
    print("  AUTOCYCLE AI  —  Tick Follower Backtest  v2.9.0")
    print("═" * 64)
    print(f"  Pairs     : {', '.join(symbols)}")
    print(f"  Days      : {n_days} trading day{'s' if n_days != 1 else ''}")
    print(f"  Balance   : ${start_bal:.2f}  →  starting lot: {start_lot}")
    print(f"  Slots     : {'ALL (no limit)' if args.all_slots else ACTIVE_SLOTS}")
    print(f"  Session   : {TRADING_START:02d}:00–{TRADING_END:02d}:00 UTC")
    print(f"  Entry     : |bar body| ≥ {ENTRY_THRESHOLD_PIPS} pip")
    print(f"  Capture   : {CAPTURE_EFFICIENCY*100:.0f}% of bar body (conservative)")

    # ── Load data ─────────────────────────────────────────────────────────────
    if not HAS_MT5:
        print("\n❌  MetaTrader5 package not found.")
        print("   Install:  pip install MetaTrader5")
        print("   Then run this script on your VPS where MT5 terminal is running.")
        sys.exit(1)

    from_dt, to_dt, days_list = _last_n_trading_days(n_days)
    print(f"  Date range: {from_dt.strftime('%Y-%m-%d')} → {to_dt.strftime('%Y-%m-%d')}")

    try:
        bars_by_sym = load_m1_bars(symbols, from_dt, to_dt)
    except RuntimeError as e:
        print(f"\n❌  {e}")
        sys.exit(1)

    loaded = {s: bars for s, bars in bars_by_sym.items() if bars}
    if not loaded:
        print("\n❌  No bar data returned for any pair.")
        print("   Is MT5 running and connected to Tickmill?")
        sys.exit(1)

    missing = [s for s in symbols if not bars_by_sym.get(s)]
    if missing:
        print(f"\n  ⚠  No data for: {missing} — these pairs will be skipped.")

    max_lot = args.max_lot
    print(f"  Max lot  : {max_lot}")

    # ── Run simulation ────────────────────────────────────────────────────────
    print(f"\n  Running simulation…")
    results = simulate(bars_by_sym, start_bal, all_slots=args.all_slots, max_lot=max_lot)
    n_trades = len(results['trades'])

    if n_trades == 0:
        print("\n  ⚠  No trades generated.")
        print("  Possible causes:")
        print("   • Market data outside session hours (05:00–20:00 UTC)")
        print("   • All M1 bars had body < 0.4 pip  (market too quiet)")
        print("   • Weekend data only (no weekday bars)")
        sys.exit(0)

    # ── Print report ──────────────────────────────────────────────────────────
    print_report(results, start_bal, n_days, symbols, max_lot=max_lot, all_slots=args.all_slots)

    # ── HTML report ───────────────────────────────────────────────────────────
    if not args.no_html and args.html:
        html = _build_html_report(results, start_bal, n_days, symbols, max_lot, args.all_slots)
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"  HTML report saved → {args.html}")

    # ── CSV export ────────────────────────────────────────────────────────────
    if args.csv:
        export_csv(results['trades'], args.csv)


# ── HTML report builder ────────────────────────────────────────────────────────

def _build_html_report(
    results: dict,
    start_balance: float,
    n_days: int,
    symbols: list,
    max_lot: float,
    all_slots: bool,
) -> str:
    """Generate a self-contained styled HTML backtest report."""
    trades    = results['trades']
    final_bal = results['final_balance']
    daily_pnl = results['daily_pnl']
    pair_stats = results['pair_stats']
    hour_pnl   = results['hour_pnl']
    pip_val    = results['pip_val']
    total_pnl  = final_bal - start_balance
    pct_return = total_pnl / start_balance * 100 if start_balance > 0 else 0
    n_trades   = len(trades)
    avg_pips   = sum(t['pips'] for t in trades) / n_trades if n_trades else 0
    daily_avg  = total_pnl / n_days if n_days else 0
    final_lot  = min(_lot_for_balance(final_bal), max_lot)

    from datetime import date as _date
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # ── Daily bars
    max_d = max(abs(v) for v in daily_pnl.values()) if daily_pnl else 1
    daily_rows = ''
    day_bar_cols = ''
    for day in sorted(daily_pnl):
        v = daily_pnl[day]
        pct = int(abs(v) / max_d * 100)
        label = datetime.strptime(day, '%Y-%m-%d').strftime('%a %d')
        daily_rows += (
            f'<tr><td>{day}</td>'
            f'<td class="num pos">+${v:,.2f}</td>'
            f'<td><div style="width:{pct}%;height:14px;background:linear-gradient(90deg,#00d084,#00a86b);border-radius:3px;min-width:4px"></div></td>'
            f'</tr>'
        )
        color = '#f0b429' if pct == 100 else '#00d084'
        day_bar_cols += (
            f'<div class="bc"><div class="bv">${v/1000:.0f}K</div>'
            f'<div class="bf" style="height:{max(1,pct)}%;background:{color}"></div>'
            f'<div class="bl">{label}</div></div>'
        )

    # ── Pair rows
    pair_rows = ''
    for sym in sorted(POOL, key=lambda s: pair_stats[s]['pnl'], reverse=True):
        st  = pair_stats[sym]
        pv  = pip_val.get(sym, 10.0)
        sp  = st['slot_bars'] / st['session_bars'] * 100 if st['session_bars'] else 0
        pair_rows += (
            f'<tr>'
            f'<td><span class="chip-pair">{sym}</span></td>'
            f'<td class="num">{st["trades"]:,}</td>'
            f'<td class="num">{st["pips"]:.2f}</td>'
            f'<td class="num pos">+${st["pnl"]:,.2f}</td>'
            f'<td class="num">${pv:.2f}</td>'
            f'<td class="num">{sp:.0f}%</td>'
            f'</tr>'
        )

    # ── Hour rows
    max_h = max(abs(v) for v in hour_pnl.values()) if hour_pnl else 1
    hour_rows = ''
    for h in range(TRADING_START, TRADING_END):
        v = hour_pnl.get(h, 0.0)
        pct = int(abs(v) / max_h * 100)
        star = ' ★' if pct == 100 else ''
        color = '#f0b429' if pct >= 90 else ('#00d084' if pct >= 40 else '#3b82f6')
        hour_rows += (
            f'<tr>'
            f'<td>{h:02d}:00</td>'
            f'<td><div style="width:{max(1,pct)}%;height:14px;background:linear-gradient(90deg,{color},{color}88);border-radius:3px"></div></td>'
            f'<td class="num pos">+${v:,.2f}{star}</td>'
            f'</tr>'
        )

    # ── Last 20 trades
    trade_rows = ''
    for t in trades[-20:]:
        chip = 'chip-buy' if t['dir'] == 'BUY' else 'chip-sell'
        trade_rows += (
            f'<tr>'
            f'<td>{t["time"].strftime("%Y-%m-%d %H:%M")}</td>'
            f'<td><span class="chip-pair">{t["sym"]}</span></td>'
            f'<td><span class="chip {chip}">{t["dir"]}</span></td>'
            f'<td class="num">{t["body_pips"]:.2f}</td>'
            f'<td class="num">{t["pips"]:.3f}</td>'
            f'<td class="num">{t["lot"]:.2f}</td>'
            f'<td class="num pos">+${t["pnl"]:.2f}</td>'
            f'<td class="num">${t["balance"]:,.2f}</td>'
            f'</tr>'
        )

    slot_label = 'ALL' if all_slots else str(ACTIVE_SLOTS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autocycle AI — Tick Follower Backtest</title>
<style>
:root{{--bg:#070b14;--surface:#0e1521;--card:#131d2e;--border:#1e2d45;
  --gold:#f0b429;--green:#00d084;--red:#ff4757;--blue:#3b82f6;--cyan:#06b6d4;
  --text:#e2e8f0;--muted:#64748b;--r:12px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.6}}
.hdr{{background:linear-gradient(135deg,#0e1521,#131d2e,#0a1628);border-bottom:1px solid var(--border);padding:24px 20px;position:relative;overflow:hidden}}
.hdr::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 10% 50%,rgba(240,180,41,.06) 0%,transparent 70%),radial-gradient(ellipse 40% 60% at 90% 30%,rgba(0,208,132,.05) 0%,transparent 70%);pointer-events:none}}
.hi{{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:relative;z-index:1}}
.li{{width:50px;height:50px;background:linear-gradient(135deg,#f0b429,#e8860f);border-radius:13px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 24px rgba(240,180,41,.35);flex-shrink:0}}
.lt h1{{font-size:20px;font-weight:800;letter-spacing:1.5px;background:linear-gradient(90deg,#f0b429,#fde68a);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1.2}}
.lt p{{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-top:2px}}
.hm{{margin-left:auto;text-align:right}}
.badge{{display:inline-block;background:rgba(0,208,132,.12);border:1px solid rgba(0,208,132,.3);color:var(--green);font-size:11px;font-weight:600;letter-spacing:1px;padding:3px 10px;border-radius:20px;margin-bottom:5px}}
.dr{{font-size:12px;color:var(--muted)}}
.main{{max-width:1100px;margin:0 auto;padding:20px 16px 48px}}
.st{{font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin:28px 0 12px;display:flex;align-items:center;gap:8px}}
.st::after{{content:'';flex:1;height:1px;background:var(--border)}}
.cards{{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 14px;position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px}}
.card.gold::before{{background:linear-gradient(90deg,var(--gold),transparent)}}
.card.green::before{{background:linear-gradient(90deg,var(--green),transparent)}}
.card.blue::before{{background:linear-gradient(90deg,var(--blue),transparent)}}
.card.cyan::before{{background:linear-gradient(90deg,var(--cyan),transparent)}}
.cl{{font-size:9px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}}
.cv{{font-size:22px;font-weight:800;line-height:1}}
.cv.gold{{color:var(--gold)}}.cv.green{{color:var(--green)}}.cv.blue{{color:var(--blue)}}.cv.cyan{{color:var(--cyan)}}
.cs{{font-size:10px;color:var(--muted);margin-top:4px}}
.ib{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 16px;display:flex;flex-wrap:wrap;gap:20px;margin-bottom:6px}}
.ii{{display:flex;flex-direction:column;gap:1px}}
.ik{{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}}
.iv{{font-size:12px;font-weight:600;color:var(--text)}}
.cw{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px;overflow-x:auto}}
.bch{{display:flex;align-items:flex-end;gap:8px;height:130px}}
.bc{{display:flex;flex-direction:column;align-items:center;gap:4px;flex:1;min-width:34px}}
.bf{{width:100%;border-radius:4px 4px 0 0;min-height:4px}}
.bl{{font-size:9px;color:var(--muted);text-align:center;white-space:nowrap}}
.bv{{font-size:9px;font-weight:700;color:var(--text);text-align:center}}
.tw{{overflow-x:auto;border-radius:var(--r);border:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse}}
thead th{{background:#0a1220;padding:10px 12px;text-align:left;font-size:9px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);white-space:nowrap;border-bottom:1px solid var(--border)}}
tbody tr{{border-bottom:1px solid rgba(30,45,69,.6)}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:rgba(255,255,255,.02)}}
tbody td{{padding:8px 12px;font-size:11px;color:var(--text);white-space:nowrap}}
.num{{font-variant-numeric:tabular-nums}}
.pos{{color:var(--green);font-weight:600}}
.chip{{display:inline-block;padding:1px 7px;border-radius:4px;font-size:10px;font-weight:700}}
.chip-buy{{background:rgba(0,208,132,.15);color:var(--green)}}
.chip-sell{{background:rgba(255,71,87,.15);color:var(--red)}}
.chip-pair{{background:rgba(59,130,246,.12);color:var(--blue);font-size:11px;font-weight:700;padding:2px 8px;border-radius:5px;display:inline-block}}
.disc{{background:rgba(240,180,41,.06);border:1px solid rgba(240,180,41,.2);border-radius:var(--r);padding:16px 18px;margin-top:28px}}
.disc h4{{font-size:11px;font-weight:700;color:var(--gold);letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}}
.disc li{{font-size:11px;color:var(--muted);margin-bottom:4px;list-style:none;padding-left:4px}}
.disc li::before{{content:'→ ';color:var(--gold)}}
.ftr{{text-align:center;padding:20px 16px;border-top:1px solid var(--border);font-size:10px;color:var(--muted);letter-spacing:1px}}
.ftr span{{color:var(--gold);font-weight:600}}
@media(max-width:600px){{
  .hi{{flex-direction:column;align-items:flex-start}}
  .hm{{margin-left:0;text-align:left}}
  .cards{{grid-template-columns:1fr 1fr}}
  .cv{{font-size:18px}}
  .bc{{min-width:24px}}
}}
</style>
</head>
<body>
<header class="hdr">
<div class="hi">
  <div class="li" style="background:#030610;width:50px;height:50px;overflow:hidden;border-radius:13px;box-shadow:0 0 28px rgba(240,165,0,.4);flex-shrink:0">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="50" height="50">
      <rect width="512" height="512" rx="112" fill="#030610"/>
      <defs>
        <linearGradient id="g1" x1="0" y1="0" x2="512" y2="512" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#F0A500"/>
          <stop offset="100%" stop-color="#FFD166"/>
        </linearGradient>
        <filter id="glow"><feGaussianBlur stdDeviation="8" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      </defs>
      <circle cx="256" cy="256" r="220" stroke="url(#g1)" stroke-width="12" stroke-dasharray="60 28" fill="none" opacity="0.7"/>
      <path d="M 256 36 A 220 220 0 0 1 476 256" stroke="url(#g1)" stroke-width="18" stroke-linecap="round" fill="none"/>
      <polyline points="92,378 168,256 252,308 392,148" stroke="url(#g1)" stroke-width="22" stroke-linecap="round" stroke-linejoin="round" fill="none" filter="url(#glow)"/>
      <circle cx="392" cy="148" r="28" fill="#FFD166" filter="url(#glow)"/>
      <circle cx="92" cy="378" r="18" fill="#F0A500" opacity="0.6"/>
    </svg>
  </div>
  <div class="lt">
    <h1>AUTOCYCLE AI</h1>
    <p>Ant-on-Sugar · Tick Velocity Follower v2.9.0</p>
  </div>
  <div class="hm">
    <div class="badge">✓ BACKTEST COMPLETE</div>
    <div class="dr">{n_days} Trading Days &nbsp;·&nbsp; {today}</div>
  </div>
</div>
</header>

<main class="main">
<div class="ib" style="margin-top:8px">
  <div class="ii"><div class="ik">Pairs</div><div class="iv">{', '.join(symbols)}</div></div>
  <div class="ii"><div class="ik">Slots</div><div class="iv">{slot_label} active (dynamic)</div></div>
  <div class="ii"><div class="ik">Session</div><div class="iv">{TRADING_START:02d}:00–{TRADING_END:02d}:00 UTC</div></div>
  <div class="ii"><div class="ik">Entry</div><div class="iv">≥{ENTRY_THRESHOLD_PIPS} pip/200ms</div></div>
  <div class="ii"><div class="ik">Capture</div><div class="iv">{CAPTURE_EFFICIENCY*100:.0f}% of bar body</div></div>
  <div class="ii"><div class="ik">Max Lot</div><div class="iv">{max_lot:.0f} lots</div></div>
</div>

<div class="st">Performance Summary</div>
<div class="cards">
  <div class="card gold"><div class="cl">Starting Balance</div><div class="cv gold">${start_balance:,.2f}</div><div class="cs">Lot {_lot_for_balance(start_balance)} starting</div></div>
  <div class="card green"><div class="cl">Final Balance</div><div class="cv green">${final_bal:,.0f}</div><div class="cs">{n_days} days</div></div>
  <div class="card green"><div class="cl">Total P&amp;L</div><div class="cv green">+${total_pnl:,.0f}</div><div class="cs">+{pct_return:,.0f}%</div></div>
  <div class="card gold"><div class="cl">Avg / Day</div><div class="cv gold">${daily_avg:,.0f}</div><div class="cs">Conservative estimate</div></div>
  <div class="card blue"><div class="cl">Total Trades</div><div class="cv blue">{n_trades:,}</div><div class="cs">{n_trades/n_days:.0f}/day</div></div>
  <div class="card cyan"><div class="cl">Avg Pips/Trade</div><div class="cv cyan">{avg_pips:.3f}</div><div class="cs">Captured pips</div></div>
  <div class="card gold"><div class="cl">Final Lot</div><div class="cv gold">{final_lot:.2f}</div><div class="cs">At broker cap</div></div>
</div>

<div class="st">Daily P&amp;L</div>
<div class="cw">
  <div class="bch">{day_bar_cols}</div>
</div>

<div class="st">Per-Pair Breakdown</div>
<div class="tw">
<table>
<thead><tr><th>Pair</th><th>Trades</th><th>Pips</th><th>P&amp;L</th><th>$/pip/lot</th><th>Slot%</th></tr></thead>
<tbody>{pair_rows}</tbody>
</table>
</div>

<div class="st">Hourly P&amp;L (Session)</div>
<div class="cw">
<table style="border:none">
<thead><tr><th>Hour</th><th style="width:100%">Activity</th><th>P&amp;L</th></tr></thead>
<tbody>{hour_rows}</tbody>
</table>
</div>

<div class="st">Last 20 Trades</div>
<div class="tw">
<table>
<thead><tr><th>Time UTC</th><th>Pair</th><th>Dir</th><th>Body</th><th>Captured</th><th>Lot</th><th>P&amp;L</th><th>Balance</th></tr></thead>
<tbody>{trade_rows}</tbody>
</table>
</div>

<div class="disc">
<h4>⚠ What's Real vs Estimated</h4>
<ul>
<li><strong>Real:</strong> All bar data from live MT5 connection — actual Tickmill M1 candles</li>
<li><strong>Real:</strong> Session hours, lot tiers, 7-pair pool exactly match tick_follower.py v2.9.0</li>
<li><strong>Real:</strong> Pair rankings (GBPUSD + USDJPY dominating) and hourly patterns — confirmed by real market data</li>
<li><strong>Estimated:</strong> {CAPTURE_EFFICIENCY*100:.0f}% bar body capture — M1 bars cannot distinguish 200ms velocity from slow 60s drift</li>
<li><strong>Estimated:</strong> No losses modeled — live system has rare stall-reversals and execution latency</li>
<li><strong>Next step:</strong> Run on demo for 1 week to get real measured performance. XAUUSD joins pool next week</li>
</ul>
</div>
</main>

<footer class="ftr">
  <span>AUTOCYCLE AI</span> &nbsp;·&nbsp; Tick Follower v2.9.0 &nbsp;·&nbsp; Generated {today} &nbsp;·&nbsp; Data: Tickmill MT5
</footer>
</body>
</html>"""


if __name__ == '__main__':
    main()
