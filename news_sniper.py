"""
AutoCycle News Sniper
=====================
Activates 15 seconds after a HIGH-impact news event fires.

Strategy:
  1. Wait just 15s for the first real candle to form
  2. Detect displacement direction (which way is price actually moving?)
  3. Find the fresh Order Block just before the spike candle
  4. Enter at MARKET PRICE in the spike direction — ride the move NOW
  5. SL = just beyond OB boundary (price should not return there if move is real)
  6. TP = 1.5× ATR (news spikes travel fast and far)

Only works on the pairs most affected by the fired news event.
"""

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────

DISPLACEMENT_THRESH = 0.8   # ATR multiplier — lowered so smaller spikes qualify
TP_ATR_CAP          = 1.5   # TP cap in ATR units — news spikes travel further
SL_ATR_BUFFER       = 0.25  # SL buffer beyond OB
MIN_RR              = 1.2   # minimum risk:reward to fire (relaxed for fast entry)

# Timeframes to check for news signals (faster TFs catch the move first)
NEWS_TFS = ['M1', 'M3', 'M5']

TIMEFRAME_MAP = {
    'M1':  mt5.TIMEFRAME_M1,
    'M3':  mt5.TIMEFRAME_M3,
    'M5':  mt5.TIMEFRAME_M5,
    'M15': mt5.TIMEFRAME_M15,
}


# ── MT5 helpers ─────────────────────────────────────────────────────────────────

def _get_candles(symbol: str, tf_str: str, n: int = 60) -> pd.DataFrame:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    tf    = TIMEFRAME_MAP.get(tf_str, mt5.TIMEFRAME_M5)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        raise ValueError(f"No data for {symbol} {tf_str}")
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df[['time', 'open', 'high', 'low', 'close']].reset_index(drop=True)


def _current_price(symbol: str) -> float:
    if not mt5.initialize():
        return 0.0
    tick = mt5.symbol_info_tick(symbol)
    return (tick.bid + tick.ask) / 2 if tick else 0.0


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 1.0
    return float(np.mean(tr[-period:]))


# ── Detection logic ─────────────────────────────────────────────────────────────

def _detect_news_displacement(df: pd.DataFrame, atr: float) -> dict:
    """
    Detect the CURRENT displacement direction at the time the sniper runs
    (i.e. ~75 seconds after news fires).

    Strategy:
      - Look at the LAST 2 candles first (most recent market direction).
        If either is a strong displacement candle (≥ 1.2× ATR body), use it.
        This captures both continuations AND reversals — whatever is happening
        RIGHT NOW at T+75s, not what happened during the initial spike.
      - If the last 2 candles are quiet (settling noise), fall back to the
        strongest candle in the last 5 — the initial spike is the move.
      - If the last 2 candles show OPPOSING displacements (reversal confirmed),
        the most recent one wins and we flag it as a reversal.
    """
    n      = len(df)
    opens  = df['open'].values
    closes = df['close'].values
    thresh = DISPLACEMENT_THRESH * atr

    # ── Step 1: Check last 2 candles (current direction) ─────────────────
    recent_disps = []
    for i in range(max(0, n - 2), n):
        body = abs(closes[i] - opens[i])
        if body < thresh:
            continue
        dirn = 'bullish' if closes[i] > opens[i] else 'bearish'
        recent_disps.append({
            'found':         True,
            'direction':     dirn,
            'idx':           i,
            'magnitude':     round(body, 4),
            'magnitude_atr': round(body / atr, 2),
            'is_reversal':   False,
        })

    if len(recent_disps) == 2:
        # Two strong candles in last 2 — check if they oppose each other
        first, last = recent_disps[0], recent_disps[1]
        if first['direction'] != last['direction']:
            # Confirmed reversal — most recent candle is the real direction
            last['is_reversal'] = True
            return last
        # Same direction — continuation, use the stronger one
        return last if last['magnitude'] >= first['magnitude'] else first

    if len(recent_disps) == 1:
        # One recent displacement — check if it opposes the initial spike
        # (look at the strongest candle in last 5 for spike direction)
        spike = None
        for i in range(max(0, n - 5), n - 2):
            body = abs(closes[i] - opens[i])
            if body < thresh:
                continue
            dirn = 'bullish' if closes[i] > opens[i] else 'bearish'
            if spike is None or body > spike['magnitude']:
                spike = {'direction': dirn, 'magnitude': body}

        recent = recent_disps[0]
        if spike and spike['direction'] != recent['direction']:
            recent['is_reversal'] = True   # current candle opposes spike = reversal
        return recent

    # ── Step 2: Fallback — no strong recent candle, use strongest in last 5 ─
    best = None
    for i in range(max(0, n - 5), n):
        body = abs(closes[i] - opens[i])
        if body < thresh:
            continue
        dirn = 'bullish' if closes[i] > opens[i] else 'bearish'
        if best is None or body > best['magnitude']:
            best = {
                'found':         True,
                'direction':     dirn,
                'idx':           i,
                'magnitude':     round(body, 4),
                'magnitude_atr': round(body / atr, 2),
                'is_reversal':   False,
            }

    return best or {'found': False, 'direction': 'none', 'magnitude': 0}


def _detect_news_ob(df: pd.DataFrame, displacement: dict) -> dict | None:
    """
    News OB = last candle opposite to displacement direction,
    found in the 3 candles immediately before the displacement candle.
    """
    if not displacement.get('found'):
        return None

    disp_idx  = displacement['idx']
    direction = displacement['direction']
    opens     = df['open'].values
    closes    = df['close'].values
    highs     = df['high'].values
    lows      = df['low'].values

    for i in range(disp_idx - 1, max(0, disp_idx - 4), -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]

        if direction == 'bullish' and is_bearish:
            ob_h = float(max(opens[i], closes[i]))
            ob_l = float(min(opens[i], closes[i]))
            return {'type': 'bullish', 'high': round(ob_h, 4),
                    'low': round(ob_l, 4), 'mid': round((ob_h + ob_l) / 2, 4)}

        elif direction == 'bearish' and is_bullish:
            ob_h = float(max(opens[i], closes[i]))
            ob_l = float(min(opens[i], closes[i]))
            return {'type': 'bearish', 'high': round(ob_h, 4),
                    'low': round(ob_l, 4), 'mid': round((ob_h + ob_l) / 2, 4)}

    return None


def _compute_tp(df: pd.DataFrame, direction: str, entry: float, atr: float) -> float:
    """Nearest swing pool beyond entry, capped at TP_ATR_CAP × ATR."""
    cap    = atr * TP_ATR_CAP
    highs  = df['high'].values
    lows   = df['low'].values
    n      = len(df)
    targets = []

    if direction == 'bullish':
        for i in range(max(0, n - 20), n - 1):
            if highs[i] > entry and highs[i] <= entry + cap * 1.5:
                targets.append(highs[i])
        if targets:
            return round(min(targets), 4)
        return round(entry + cap, 4)
    else:
        for i in range(max(0, n - 20), n - 1):
            if lows[i] < entry and lows[i] >= entry - cap * 1.5:
                targets.append(lows[i])
        if targets:
            return round(max(targets), 4)
        return round(entry - cap, 4)


# ── Main analysis ────────────────────────────────────────────────────────────────

def analyze_news_snipe(symbol: str, tf_str: str, news_event: dict) -> dict:
    """
    Run post-news sniper analysis on one symbol + timeframe.

    Returns a result dict with verdict: 'FIRE', 'WATCH', or 'SKIP'.
    """
    base = {
        'symbol':     symbol,
        'tf':         tf_str,
        'news_title': news_event.get('title', ''),
        'news_country': news_event.get('country', ''),
        'verdict':    'SKIP',
        'direction':  'none',
        'entry': None, 'sl': None, 'tp': None, 'rr': None,
    }

    try:
        df      = _get_candles(symbol, tf_str, n=60)
        atr     = _atr(df)
        live    = _current_price(symbol)

        # ── Step 1: Was there a real displacement? ────────────────────────
        displacement = _detect_news_displacement(df, atr)
        if not displacement.get('found'):
            base['reason'] = 'No real displacement — news had no follow-through'
            return base

        direction = displacement['direction']

        # ── Step 2: Find news OB ──────────────────────────────────────────
        ob = _detect_news_ob(df, displacement)
        if ob is None:
            base['reason'] = 'No OB found near displacement'
            return base

        # ── Step 3: Trade levels — market-price entry ─────────────────────
        # At T+15s the spike is still running. Enter at current market price
        # (not OB mid — that would require waiting for a retrace that may never
        # come). OB boundary still serves as the SL anchor: if price returns
        # there the move has failed.
        entry  = live
        sl_buf = atr * SL_ATR_BUFFER

        if direction == 'bullish':
            sl_ob = round(ob['low'] - sl_buf, 4)
            # Hard cap: SL never more than 2× ATR from entry
            sl = max(sl_ob, round(entry - atr * 2.0, 4))
            if sl >= entry:
                sl = round(entry - atr * 0.5, 4)
        else:
            sl_ob = round(ob['high'] + sl_buf, 4)
            sl = min(sl_ob, round(entry + atr * 2.0, 4))
            if sl <= entry:
                sl = round(entry + atr * 0.5, 4)

        risk = abs(entry - sl)
        if risk == 0:
            base['reason'] = 'Zero risk — degenerate OB'
            return base

        # TP: fixed ATR cap for fast spike entry (no swing-pool search needed)
        if direction == 'bullish':
            tp = round(entry + atr * TP_ATR_CAP, 4)
        else:
            tp = round(entry - atr * TP_ATR_CAP, 4)

        rr = round(abs(tp - entry) / risk, 2)

        if rr < MIN_RR:
            base['reason'] = f'RR {rr} < minimum {MIN_RR}'
            return base

        # ── Step 4: Always FIRE — we enter market on the spike ────────────
        # No price_at_ob check needed; we're riding the move, not waiting for
        # a retrace. If conditions are met, FIRE immediately.
        is_reversal = displacement.get('is_reversal', False)

        return {
            **base,
            'direction':    direction,
            'verdict':      'FIRE',
            'is_reversal':  is_reversal,
            'entry':        round(entry, 4),
            'sl':           round(sl, 4),
            'tp':           round(tp, 4),
            'rr':           rr,
            'price':        round(live, 4),
            'price_at_ob':  False,
            'atr':          round(atr, 4),
            'displacement': displacement,
            'ob':           ob,
            'entry_zone':   {'low': ob['low'], 'high': ob['high'], 'type': 'NewsOB'},
        }

    except Exception as exc:
        log.warning(f"NewsSniper {symbol} {tf_str}: {exc}")
        base['reason'] = str(exc)
        return base


def run_sniper_for_event(news_event: dict) -> list:
    """
    Run the news sniper on all pairs affected by the given event.
    Returns a list of FIRE or WATCH results (SKIP results are dropped).
    """
    pairs   = news_event.get('pairs', [])
    results = []

    for symbol in pairs:
        for tf in NEWS_TFS:
            try:
                result = analyze_news_snipe(symbol, tf, news_event)
                if result['verdict'] in ('FIRE', 'WATCH'):
                    results.append(result)
                    if result['verdict'] == 'FIRE':
                        break   # got a FIRE on this symbol — skip lower TFs
            except Exception as exc:
                log.warning(f"run_sniper_for_event {symbol} {tf}: {exc}")

    return results
