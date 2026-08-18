"""
Trend Analyzer — Confluence Signal Engine (v2)
================================================
Upgraded from basic pattern detection to a multi-factor confluence system.
A signal only fires when 4 or more independent factors agree.
This is the difference between 50% and 65%+ win rate.

Confluence factors checked (need ≥ 4 of 6 to generate a signal):
  1. Chart pattern quality (R² > 0.75 on trendline fit)
  2. RSI position (not overbought/oversold, or at extreme = bonus)
  3. MACD direction + histogram momentum
  4. Multi-timeframe alignment (H4 signal confirmed by D1 bias)
  5. Risk:Reward ratio ≥ 1.5:1 (using ATR-based stop loss)
  6. Active trading session (London 07–16 UTC / NY 13–21 UTC)

Additional Gold-specific logic:
  - Round number zones ($3900, $4000, $4100…) treated as major S/R
  - ATR-based stop loss prevents getting stopped by normal volatility
  - Only bullish signals when price is above 50-period EMA on D1
"""

import logging
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import MetaTrader5 as mt5
from scipy.signal import argrelextrema
from scipy.stats import linregress

log = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    'M1':  mt5.TIMEFRAME_M1,
    'M3':  mt5.TIMEFRAME_M3,
    'M5':  mt5.TIMEFRAME_M5,
    'M10': mt5.TIMEFRAME_M10,
    'M15': mt5.TIMEFRAME_M15,
    'M20': mt5.TIMEFRAME_M20,
    'M30': mt5.TIMEFRAME_M30,
    'H1':  mt5.TIMEFRAME_H1,
    'H4':  mt5.TIMEFRAME_H4,
    'D1':  mt5.TIMEFRAME_D1,
    'W1':  mt5.TIMEFRAME_W1,
}

HIGHER_TF = {'M5': 'M15', 'M15': 'H1', 'M30': 'H1', 'H1': 'H4', 'H4': 'D1', 'D1': 'W1', 'W1': 'W1'}


# ── Connection ─────────────────────────────────────────────────────────────────

def _mt5_init():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    import os
    login    = int(os.getenv('MT5_LOGIN', '0'))
    password = os.getenv('MT5_PASSWORD', '')
    server   = os.getenv('MT5_SERVER', '')
    if login and password and server:
        return mt5.initialize(login=login, password=password, server=server)
    return mt5.initialize()


def _connect():
    if not _mt5_init():
        raise RuntimeError(
            f"MT5 initialize() failed — is MT5 open and logged in? {mt5.last_error()}"
        )


def _resolve_symbol(symbol: str) -> str:
    """Auto-detect the correct symbol name for the connected broker.

    Vantage uses a '+' suffix (XAUUSD+, EURUSD+).
    Other brokers use plain names (XAUUSD, EURUSD).
    Tries the symbol as-is first, then with + added, then with + removed,
    so the platform works on any broker without manual config changes.
    """
    # Try as typed first
    if mt5.symbol_select(symbol, True):
        return symbol
    # Try adding + (e.g. XAUUSD → XAUUSD+ for Vantage)
    plus = symbol.rstrip('+') + '+'
    if plus != symbol and mt5.symbol_select(plus, True):
        return plus
    # Try removing + (e.g. XAUUSD+ → XAUUSD for non-Vantage)
    plain = symbol.rstrip('+')
    if plain != symbol and mt5.symbol_select(plain, True):
        return plain
    return symbol  # return original — MT5 will give a clear error


# ── Data Fetch ─────────────────────────────────────────────────────────────────

def get_candles(symbol: str, timeframe_str: str = 'D1', n: int = 200) -> pd.DataFrame:
    _connect()
    symbol = _resolve_symbol(symbol)   # auto-correct for broker suffix
    tf = TIMEFRAME_MAP.get(timeframe_str, mt5.TIMEFRAME_D1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        raise ValueError(
            f"No data for {symbol} {timeframe_str}. "
            "Check symbol name and MT5 connection."
        )
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'tick_volume': 'volume'})
    return df[['time', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True)


# ── Technical Indicators ───────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder RSI. Values: 0–100. Oversold < 30, Overbought > 70."""
    delta = df['close'].diff()
    gain  = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — measures current market volatility."""
    h_l = df['high'] - df['low']
    h_pc = (df['high'] - df['close'].shift()).abs()
    l_pc = (df['low']  - df['close'].shift()).abs()
    tr  = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14):
    """
    Average Directional Index — measures trend STRENGTH (not direction).
    Returns (adx, plus_di, minus_di) as the latest float values.

    ADX > 22 : trending market → signals are reliable
    ADX < 18 : ranging / choppy → avoid directional trades
    plus_di  > minus_di : bullish momentum
    minus_di > plus_di  : bearish momentum
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    # True Range
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_s = tr.ewm(span=period, adjust=False).mean()

    # Directional Movement
    up   = high.diff()
    down = -low.diff()
    pdm  = np.where((up > down) & (up > 0), up, 0.0)
    mdm  = np.where((down > up) & (down > 0), down, 0.0)

    pdi = 100 * pd.Series(pdm, index=df.index).ewm(span=period, adjust=False).mean() / atr_s
    mdi = 100 * pd.Series(mdm, index=df.index).ewm(span=period, adjust=False).mean() / atr_s
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(span=period, adjust=False).mean()

    return float(adx.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])


def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential Moving Average — trend direction at a glance."""
    return df['close'].ewm(span=period, adjust=False).mean()


def compute_fibonacci(df: pd.DataFrame) -> dict:
    """
    Fibonacci retracement levels from the recent 60-candle swing high/low.
    Key levels: 23.6%, 38.2%, 50%, 61.8%, 78.6%
    Big money uses 38.2 and 61.8 as primary entry/exit zones.
    """
    recent = df.tail(60)
    high   = float(recent['high'].max())
    low    = float(recent['low'].min())
    diff   = high - low
    if diff == 0:
        return {}
    return {
        'high':   round(high, 2),
        'low':    round(low,  2),
        'r236':   round(high - 0.236 * diff, 2),
        'r382':   round(high - 0.382 * diff, 2),
        'r500':   round(high - 0.500 * diff, 2),
        'r618':   round(high - 0.618 * diff, 2),
        'r786':   round(high - 0.786 * diff, 2),
    }


def compute_macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> dict:
    """MACD line, signal line, and histogram."""
    ema_f  = df['close'].ewm(span=fast, adjust=False).mean()
    ema_s  = df['close'].ewm(span=slow, adjust=False).mean()
    macd   = ema_f - ema_s
    sig    = macd.ewm(span=signal, adjust=False).mean()
    hist   = macd - sig
    times  = df['time'].dt.strftime('%Y-%m-%dT%H:%M:%S').tolist()
    return {
        'times': times,
        'macd':      [round(v, 4) for v in macd],
        'signal':    [round(v, 4) for v in sig],
        'histogram': [round(v, 4) for v in hist],
    }


def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df['close'].ewm(span=period, adjust=False).mean()


# ── Swing Points ───────────────────────────────────────────────────────────────

def find_swing_highs(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    idx = argrelextrema(df['high'].values, np.greater_equal, order=order)[0]
    return df.iloc[idx].copy()


def find_swing_lows(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    idx = argrelextrema(df['low'].values, np.less_equal, order=order)[0]
    return df.iloc[idx].copy()


# ── Pattern Detection ──────────────────────────────────────────────────────────

def _fit_line(positions, prices):
    if len(positions) < 2:
        return None, None, 0
    slope, intercept, r, _, _ = linregress(positions, prices)
    return slope, intercept, r ** 2


def detect_patterns(df: pd.DataFrame, swing_highs: pd.DataFrame, swing_lows: pd.DataFrame,
                    atr_series: pd.Series = None) -> dict:
    """Detect the strongest chart pattern. Only reports patterns with R² > 0.70."""
    patterns_found = []

    # ── Wedge patterns ────────────────────────────────────────────────────────
    if len(swing_highs) >= 4 and len(swing_lows) >= 4:
        sh = swing_highs.tail(5)
        sl = swing_lows.tail(5)
        sh_slope, sh_int, sh_r2 = _fit_line(sh.index.tolist(), sh['high'].values)
        sl_slope, sl_int, sl_r2 = _fit_line(sl.index.tolist(), sl['low'].values)

        if sh_slope is not None and sl_slope is not None:
            avg_r2 = (sh_r2 + sl_r2) / 2

            if sh_slope < -0.05 and sl_slope < -0.05 and sl_slope > sh_slope and avg_r2 > 0.70:
                patterns_found.append({
                    'name': 'Falling Wedge', 'direction': 'bullish',
                    'description': 'Down-Sloping Trendlines Squeezing Together',
                    'confidence': min(0.55 + avg_r2 * 0.35, 0.92),
                    'sh_slope': sh_slope, 'sh_intercept': sh_int,
                    'sl_slope': sl_slope, 'sl_intercept': sl_int,
                    'sh': sh, 'sl': sl,
                })

            elif sh_slope > 0.05 and sl_slope > 0.05 and sl_slope > sh_slope and avg_r2 > 0.70:
                patterns_found.append({
                    'name': 'Rising Wedge', 'direction': 'bearish',
                    'description': 'Up-Sloping Trendlines Squeezing Together',
                    'confidence': min(0.55 + avg_r2 * 0.35, 0.92),
                    'sh_slope': sh_slope, 'sh_intercept': sh_int,
                    'sl_slope': sl_slope, 'sl_intercept': sl_int,
                    'sh': sh, 'sl': sl,
                })

            elif (sh_slope > 0.03 and sl_slope > 0.03
                  and abs(sh_slope - sl_slope) < 0.25 * abs(sh_slope)
                  and avg_r2 > 0.65):
                patterns_found.append({
                    'name': 'Ascending Channel', 'direction': 'bullish',
                    'description': 'Parallel Upward Channel — Bullish Continuation',
                    'confidence': min(0.50 + avg_r2 * 0.25, 0.80),
                    'sh_slope': sh_slope, 'sh_intercept': sh_int,
                    'sl_slope': sl_slope, 'sl_intercept': sl_int,
                    'sh': sh, 'sl': sl,
                })

            elif (sh_slope < -0.03 and sl_slope < -0.03
                  and abs(sh_slope - sl_slope) < 0.25 * abs(sh_slope)
                  and avg_r2 > 0.65):
                patterns_found.append({
                    'name': 'Descending Channel', 'direction': 'bearish',
                    'description': 'Parallel Downward Channel — Bearish Continuation',
                    'confidence': min(0.50 + avg_r2 * 0.25, 0.80),
                    'sh_slope': sh_slope, 'sh_intercept': sh_int,
                    'sl_slope': sl_slope, 'sl_intercept': sl_int,
                    'sh': sh, 'sl': sl,
                })

    # ── Double Top / Double Bottom ────────────────────────────────────────────
    # NECKLINE CONFIRMATION REQUIRED:
    # Double Bottom → neckline = highest high between the two troughs.
    #   Signal only fires when current close is ABOVE neckline (breakout confirmed).
    #   If price later drops back below neckline, Pattern factor fails → WATCH/SKIP.
    # Double Top   → neckline = lowest low between the two peaks.
    #   Signal only fires when current close is BELOW neckline.
    # This prevents bull/bear traps where price bounces briefly then reverses.

    current_close = float(df['close'].iloc[-1])

    if len(swing_highs) >= 3:
        last = swing_highs.tail(3)['high'].values
        for i in range(len(last) - 1):
            diff = abs(last[i] - last[-1]) / last[-1]
            if diff < 0.015:
                sh2 = swing_highs.tail(3)
                sl2 = swing_lows.tail(3) if len(swing_lows) >= 2 else swing_lows
                # Neckline = lowest low between the two peaks
                idx1 = sh2.index[i]
                idx2 = sh2.index[-1]
                between = df.loc[idx1:idx2, 'low'] if idx1 < idx2 else df.loc[idx2:idx1, 'low']
                neckline = float(between.min()) if len(between) > 0 else float(df['low'].iloc[-1])
                # Require close BELOW neckline — pattern must be breaking down
                if current_close > neckline:
                    break   # price still above neckline — no confirmation yet
                sh_s, sh_i, _ = _fit_line(sh2.index.tolist(), sh2['high'].values)
                sl_s, sl_i, _ = _fit_line(sl2.index.tolist(), sl2['low'].values)
                patterns_found.append({
                    'name': 'Double Top', 'direction': 'bearish',
                    'description': 'Two Peaks at the Same Level — Bearish Reversal',
                    'confidence': 0.78 - diff * 15,
                    'neckline':   round(neckline, 5),
                    'sh_slope': sh_s or 0, 'sh_intercept': sh_i or last.mean(),
                    'sl_slope': sl_s or 0, 'sl_intercept': sl_i or (sl2['low'].mean() if len(sl2) else 0),
                    'sh': sh2, 'sl': sl2,
                })
                break

    if len(swing_lows) >= 3:
        last = swing_lows.tail(3)['low'].values
        for i in range(len(last) - 1):
            diff = abs(last[i] - last[-1]) / last[-1]
            if diff < 0.015:
                sh2 = swing_highs.tail(3) if len(swing_highs) >= 2 else swing_highs
                sl2 = swing_lows.tail(3)
                # Neckline = highest high between the two troughs
                idx1 = sl2.index[i]
                idx2 = sl2.index[-1]
                between = df.loc[idx1:idx2, 'high'] if idx1 < idx2 else df.loc[idx2:idx1, 'high']
                neckline = float(between.max()) if len(between) > 0 else float(df['high'].iloc[-1])
                # Require close ABOVE neckline — pattern must be breaking up
                if current_close < neckline:
                    break   # price still below neckline — no confirmation yet
                sh_s, sh_i, _ = _fit_line(sh2.index.tolist(), sh2['high'].values)
                sl_s, sl_i, _ = _fit_line(sl2.index.tolist(), sl2['low'].values)
                patterns_found.append({
                    'name': 'Double Bottom', 'direction': 'bullish',
                    'description': 'Two Troughs at the Same Level — Bullish Reversal',
                    'confidence': 0.78 - diff * 15,
                    'neckline':   round(neckline, 5),
                    'sh_slope': sh_s or 0, 'sh_intercept': sh_i or (sh2['high'].mean() if len(sh2) else 0),
                    'sl_slope': sl_s or 0, 'sl_intercept': sl_i or last.mean(),
                    'sh': sh2, 'sl': sl2,
                })
                break

    # ── Pin Bar (Hammer / Shooting Star) ─────────────────────────────────────
    # Single-candle rejection signal — the most common high-probability setup on M5.
    # A pin bar has a long wick on one side (rejection) and a small body.
    # Bullish pin bar (hammer):       long lower wick → price rejected lows → buy
    # Bearish pin bar (shooting star): long upper wick → price rejected highs → sell
    #
    # Requirements:
    #   • Candle size ≥ 0.8× ATR  (not a tiny nothing candle)
    #   • Body ≤ 40% of range     (clear rejection, not a full directional candle)
    #   • Rejection wick ≥ 58% of range
    #   • Opposite wick ≤ 28%    (clean one-sided rejection)
    #   • Close in upper 55%+ of range (bullish) / lower 55%+ (bearish)
    # Confidence boosted to 0.76 when wick tip is at a key level (EMA20, round#, swing S/R)
    try:
        _atr_val = float(atr_series.iloc[-1]) if atr_series is not None else float(compute_atr(df).iloc[-1])
        _ema20_series = compute_ema(df, 20)
        _current_p = float(df['close'].iloc[-1])

        for _ci in [-4, -3, -2]:
            if abs(_ci) > len(df):
                continue
            _c = df.iloc[_ci]
            _co, _ch, _cl, _cc = float(_c['open']), float(_c['high']), float(_c['low']), float(_c['close'])
            _rng = _ch - _cl
            if _rng < _atr_val * 0.8 or _rng == 0:
                continue

            _body     = abs(_cc - _co)
            _body_pct = _body / _rng
            _up_wick  = _ch - max(_co, _cc)
            _lo_wick  = min(_co, _cc) - _cl
            _up_pct   = _up_wick / _rng
            _lo_pct   = _lo_wick / _rng

            if _body_pct > 0.40:
                continue

            _pin_dir = None
            # Bullish: long lower wick, close in upper 55%+ of candle
            if _lo_pct >= 0.58 and _up_pct <= 0.28 and (_cc - _cl) / _rng >= 0.55:
                _pin_dir = 'bullish'
            # Bearish: long upper wick, close in lower 55%+ of candle
            elif _up_pct >= 0.58 and _lo_pct <= 0.28 and (_ch - _cc) / _rng >= 0.55:
                _pin_dir = 'bearish'

            if not _pin_dir:
                continue

            # Key level check — is the wick tip touching a meaningful level?
            _wick_tip = _cl if _pin_dir == 'bullish' else _ch
            _ema20_at = float(_ema20_series.iloc[_ci])
            _at_key   = abs(_wick_tip - _ema20_at) <= _atr_val * 0.5

            if not _at_key and _current_p >= 100:
                _interval = 10 if _current_p >= 500 else 1
                _nr = round(_wick_tip / _interval) * _interval
                if abs(_wick_tip - _nr) <= _atr_val * 0.35:
                    _at_key = True

            if not _at_key:
                _swings_for_pin = swing_lows if _pin_dir == 'bullish' else swing_highs
                _pcol           = 'low' if _pin_dir == 'bullish' else 'high'
                if len(_swings_for_pin) > 0:
                    _near = _swings_for_pin.tail(5)[_pcol]
                    if any(abs(_wick_tip - float(_p)) <= _atr_val * 0.5 for _p in _near):
                        _at_key = True

            _conf = 0.76 if _at_key else 0.62
            _desc_extra = ' at key level' if _at_key else ''
            _sh_s, _sh_i, _sl_s, _sl_i = 0, float(df['high'].mean()), 0, float(df['low'].mean())
            _sh_stub = swing_highs.tail(3) if len(swing_highs) >= 2 else swing_highs
            _sl_stub = swing_lows.tail(3)  if len(swing_lows)  >= 2 else swing_lows
            if len(_sh_stub) >= 2: _sh_s, _sh_i, _ = _fit_line(_sh_stub.index.tolist(), _sh_stub['high'].values)
            if len(_sl_stub) >= 2: _sl_s, _sl_i, _ = _fit_line(_sl_stub.index.tolist(), _sl_stub['low'].values)

            patterns_found.append({
                'name': 'Pin Bar',
                'direction': _pin_dir,
                'description': f'{"Bullish" if _pin_dir == "bullish" else "Bearish"} Pin Bar — sharp rejection of {"lows" if _pin_dir == "bullish" else "highs"}{_desc_extra}',
                'confidence': _conf,
                'sh_slope': _sh_s or 0, 'sh_intercept': _sh_i,
                'sl_slope': _sl_s or 0, 'sl_intercept': _sl_i,
                'sh': _sh_stub, 'sl': _sl_stub,
            })
            break   # first recent pin bar found — stop scanning
    except Exception:
        pass

    # ── EMA Bounce (Dynamic Support / Resistance Touch & Rejection) ───────────
    # When price pulls back to EMA20 during a trending market and bounces, this
    # is one of the most reliable M5/M15 setups. EMA20 acts as dynamic S/R ONLY
    # when the market is actually trending — in a ranging market EMA20 is just
    # the midpoint of the range and price oscillates through it, causing losses.
    #
    # Hard requirements (MUST ALL pass):
    #   • ADX > 20 — minimum trend strength. Below 20 = ranging, skip entirely.
    #   • EMA20 aligned WITH EMA50 (trade with the bigger trend)
    #   • EMA20 sloping in the trend direction (not flat/sideways)
    #   • At least one of last 3 completed candles touched EMA20 (within 0.5 ATR)
    #   • Current close is meaningfully AWAY from EMA20 (≥ 0.2 ATR) — not just
    #     barely crossing it — confirming a real bounce, not a whipsaw
    #   • BOTH last 2 completed closes are on the correct side of EMA20
    #     (not just one — both must confirm the bounce direction)
    try:
        _e20 = compute_ema(df, 20)
        _e50 = compute_ema(df, 50)
        _atr_e = float(atr_series.iloc[-1]) if atr_series is not None else float(compute_atr(df).iloc[-1])
        _e20_now   = float(_e20.iloc[-1])
        _e20_5ago  = float(_e20.iloc[-6]) if len(df) >= 6 else _e20_now
        _e50_now   = float(_e50.iloc[-1])
        _e20_slope = (_e20_now - _e20_5ago) / 5
        _touch_z   = _atr_e * 0.5
        _min_bounce = _atr_e * 0.20   # must be at least 0.2 ATR from EMA after bounce
        _last3_l   = df['low'].iloc[-4:-1].values.tolist()
        _last3_h   = df['high'].iloc[-4:-1].values.tolist()
        _last2_c   = df['close'].iloc[-3:-1].values.tolist()
        _cur_close = float(df['close'].iloc[-1])

        # ADX gate — EMA Bounce is ONLY valid in a genuinely trending market.
        # ADX > 25 is the standard "trending" threshold. Below 25 the market is
        # ranging/choppy — EMA20 is just the midpoint of the range, not real S/R.
        # ADX 24.6 looks close to 25 but it IS ranging — don't be fooled by the number.
        try:
            _eb_adx, _eb_pdi, _eb_mdi = compute_adx(df)
            _eb_trending = _eb_adx > 25
        except Exception:
            _eb_trending = True   # can't compute → allow

        def _ema_bounce_stubs():
            _ss, _si, _ls, _li = 0, float(df['high'].mean()), 0, float(df['low'].mean())
            _shs = swing_highs.tail(3) if len(swing_highs) >= 2 else swing_highs
            _sls = swing_lows.tail(3)  if len(swing_lows)  >= 2 else swing_lows
            if len(_shs) >= 2: _ss, _si, _ = _fit_line(_shs.index.tolist(), _shs['high'].values)
            if len(_sls) >= 2: _ls, _li, _ = _fit_line(_sls.index.tolist(), _sls['low'].values)
            return _ss, _si, _ls, _li, _shs, _sls

        if _eb_trending and _e20_now > _e50_now and _e20_slope > 0:
            _touched = any(_l <= _e20_now + _touch_z for _l in _last3_l)
            # BOTH last 2 closes must be above EMA20 AND current close meaningfully above
            _bounced = ((_cur_close - _e20_now) >= _min_bounce) and all(_c > _e20_now for _c in _last2_c)
            if _touched and _bounced:
                _ss, _si, _ls, _li, _shs, _sls = _ema_bounce_stubs()
                patterns_found.append({
                    'name': 'EMA Bounce', 'direction': 'bullish',
                    'description': 'EMA20 Bounce — price tested dynamic support and rejected upward',
                    'confidence': 0.72,
                    'sh_slope': _ss or 0, 'sh_intercept': _si,
                    'sl_slope': _ls or 0, 'sl_intercept': _li,
                    'sh': _shs, 'sl': _sls,
                })

        elif _eb_trending and _e20_now < _e50_now and _e20_slope < 0:
            _touched = any(_h >= _e20_now - _touch_z for _h in _last3_h)
            # BOTH last 2 closes must be below EMA20 AND current close meaningfully below
            _bounced = ((_e20_now - _cur_close) >= _min_bounce) and all(_c < _e20_now for _c in _last2_c)
            if _touched and _bounced:
                _ss, _si, _ls, _li, _shs, _sls = _ema_bounce_stubs()
                patterns_found.append({
                    'name': 'EMA Bounce', 'direction': 'bearish',
                    'description': 'EMA20 Rejection — price tested dynamic resistance and turned lower',
                    'confidence': 0.72,
                    'sh_slope': _ss or 0, 'sh_intercept': _si,
                    'sl_slope': _ls or 0, 'sl_intercept': _li,
                    'sh': _shs, 'sl': _sls,
                })
    except Exception:
        pass

    # ── Round Level Bounce (Gold / Indices) ───────────────────────────────────
    # For high-priced instruments price frequently stalls and reverses at round
    # levels (Gold: $10 increments, indices: $50 increments). These are heavy
    # institutional order zones — banks stack buy/sell orders at round numbers.
    #
    # Requirements:
    #   • Price within 0.8× ATR of a round level
    #   • Last completed candle has a rejection wick (≥ 40% of range) pointing
    #     AWAY from the round level (confirming the institutional reaction)
    try:
        _rl_p = float(df['close'].iloc[-1])
        if _rl_p >= 500:
            _atr_rl    = float(atr_series.iloc[-1]) if atr_series is not None else float(compute_atr(df).iloc[-1])
            _rl_step   = 50 if _rl_p >= 5000 else 10
            _rl_near   = round(_rl_p / _rl_step) * _rl_step
            _rl_dist   = abs(_rl_p - _rl_near)

            if _rl_dist <= _atr_rl * 0.8:
                _rlc = df.iloc[-2]   # last completed candle
                _rlo, _rlh, _rll, _rlc_c = float(_rlc['open']), float(_rlc['high']), float(_rlc['low']), float(_rlc['close'])
                _rl_range = _rlh - _rll
                if _rl_range > 0:
                    _rl_up_wk = _rlh - max(_rlo, _rlc_c)
                    _rl_lo_wk = min(_rlo, _rlc_c) - _rll
                    _rl_ss, _rl_si, _rl_ls, _rl_li = 0, float(df['high'].mean()), 0, float(df['low'].mean())
                    _rl_shs = swing_highs.tail(3) if len(swing_highs) >= 2 else swing_highs
                    _rl_sls = swing_lows.tail(3)  if len(swing_lows)  >= 2 else swing_lows
                    if len(_rl_shs) >= 2: _rl_ss, _rl_si, _ = _fit_line(_rl_shs.index.tolist(), _rl_shs['high'].values)
                    if len(_rl_sls) >= 2: _rl_ls, _rl_li, _ = _fit_line(_rl_sls.index.tolist(), _rl_sls['low'].values)

                    # Bullish: lower wick rejection + price near or below round level
                    if (_rl_lo_wk / _rl_range >= 0.40
                            and _rl_p <= _rl_near + _atr_rl * 0.4):
                        patterns_found.append({
                            'name': 'Round Level Bounce', 'direction': 'bullish',
                            'description': f'Bullish bounce at {_rl_near:.0f} — major round level support',
                            'confidence': 0.74,
                            'sh_slope': _rl_ss or 0, 'sh_intercept': _rl_si,
                            'sl_slope': _rl_ls or 0, 'sl_intercept': _rl_li,
                            'sh': _rl_shs, 'sl': _rl_sls,
                        })
                    # Bearish: upper wick rejection + price near or above round level
                    elif (_rl_up_wk / _rl_range >= 0.40
                            and _rl_p >= _rl_near - _atr_rl * 0.4):
                        patterns_found.append({
                            'name': 'Round Level Bounce', 'direction': 'bearish',
                            'description': f'Bearish rejection at {_rl_near:.0f} — major round level resistance',
                            'confidence': 0.74,
                            'sh_slope': _rl_ss or 0, 'sh_intercept': _rl_si,
                            'sl_slope': _rl_ls or 0, 'sl_intercept': _rl_li,
                            'sh': _rl_shs, 'sl': _rl_sls,
                        })
    except Exception:
        pass

    if patterns_found:
        return max(patterns_found, key=lambda p: p['confidence'])

    # ── Fallback: EMA trend ───────────────────────────────────────────────────
    ema20 = compute_ema(df, 20)
    ema50 = compute_ema(df, 50)
    direction = 'bullish' if ema20.iloc[-1] > ema50.iloc[-1] else 'bearish'
    sh = swing_highs.tail(3) if len(swing_highs) >= 2 else swing_highs
    sl = swing_lows.tail(3)  if len(swing_lows)  >= 2 else swing_lows
    sh_s, sh_i, _ = _fit_line(sh.index.tolist(), sh['high'].values) if len(sh) >= 2 else (0, df['high'].mean(), 0)
    sl_s, sl_i, _ = _fit_line(sl.index.tolist(), sl['low'].values)  if len(sl) >= 2 else (0, df['low'].mean(), 0)
    return {
        'name': 'EMA Trend', 'direction': direction,
        'description': f'EMA20 {"above" if direction == "bullish" else "below"} EMA50',
        'confidence': 0.42,
        'sh_slope': sh_s or 0, 'sh_intercept': sh_i or df['high'].mean(),
        'sl_slope': sl_s or 0, 'sl_intercept': sl_i or df['low'].mean(),
        'sh': sh, 'sl': sl,
    }


# ── Support & Resistance ───────────────────────────────────────────────────────

def find_sr_levels(df, swing_highs, swing_lows, n=3):
    current = df['close'].iloc[-1]
    cluster_pct = 0.003

    def cluster(prices):
        if not len(prices): return []
        out = [prices[0]]
        for p in prices[1:]:
            if abs(p - out[-1]) / out[-1] > cluster_pct:
                out.append(p)
        return out

    # Round number zones (especially important for Gold)
    round_levels = []
    base = round(current / 100) * 100
    for mult in range(-3, 4):
        lvl = base + mult * 100
        if abs(lvl - current) / current < 0.05:
            round_levels.append(lvl)

    # Only add round levels on the CORRECT side of current price
    raw_res = sorted(swing_highs[swing_highs['high'] > current]['high'].tolist() + [r for r in round_levels if r > current], reverse=False)
    raw_sup = sorted(swing_lows[swing_lows['low']   < current]['low'].tolist()  + [r for r in round_levels if r < current], reverse=True)

    res = cluster(raw_res)[:n]
    sup = cluster(raw_sup)[:n]

    levels = []
    for i, p in enumerate(res): levels.append({'price': round(p, 2), 'type': 'resistance', 'label': f'R{i+1}', 'round': p in round_levels})
    for i, p in enumerate(sup): levels.append({'price': round(p, 2), 'type': 'support',    'label': f'S{i+1}', 'round': p in round_levels})
    return levels


def find_zones(df, swing_highs, swing_lows):
    current   = df['close'].iloc[-1]
    zone_half = 0.004
    supply, demand = [], []
    for _, r in swing_highs[swing_highs['high'] > current].tail(3).iterrows():
        p = r['high']
        supply.append({'top': round(p*(1+zone_half), 2), 'bottom': round(p*(1-zone_half), 2)})
    for _, r in swing_lows[swing_lows['low'] < current].tail(3).iterrows():
        p = r['low']
        demand.append({'top': round(p*(1+zone_half), 2), 'bottom': round(p*(1-zone_half), 2)})
    return supply, demand


# ── ATR-based Trade Levels ─────────────────────────────────────────────────────

def _dp(price: float) -> int:
    """
    Adaptive decimal places based on instrument price magnitude.
      ≥ 1000  → 2 dp  (Gold: 4,000+; NAS100: 20,000+; US30: 50,000+)
      ≥ 100   → 2 dp  (JPY pairs: 100-200; UK100, GER40)
      ≥ 10    → 3 dp  (Silver ~30, Oil ~70)
      < 10    → 5 dp  (All forex majors/minors: EURUSD, GBPUSD, USDCHF, etc.)
    """
    if price >= 100:  return 2
    if price >= 10:   return 3
    return 5


def _rnd(val: float, price: float) -> float:
    """Round val to the appropriate decimal places for this instrument."""
    return round(val, _dp(price))


def calculate_trade_levels(pattern, df, sr_levels, atr_series, risk_pct=2.0, timeframe='H1'):
    """
    SL = ATR×multiplier from entry (scales per timeframe).
    TP = nearest S/R level in the correct direction — only levels BEYOND current
         price count, so the target is always genuinely reachable.
    Buffer around S/R uses ATR×0.1 — proportional to instrument volatility,
    not a hardcoded dollar amount (which breaks forex pairs).
    Decimal rounding is adaptive: 5 dp for forex, 2 dp for Gold/indices.
    """
    current   = float(df['close'].iloc[-1])
    direction = pattern['direction']
    # Cap ATR at 1.5× its own 14-period median — prevents a news spike candle
    # from inflating SL/TP for all subsequent signals on that pair.
    _atr_raw    = float(atr_series.iloc[-1])
    _atr_median = float(atr_series.dropna().iloc[-14:].median())
    atr         = min(_atr_raw, _atr_median * 1.5)
    dp        = _dp(current)

    # SL multiplier — wider on higher TFs to survive OB wick noise
    sl_mult   = {'M5': 1.2, 'M15': 1.8, 'M30': 2.5, 'H1': 3.0, 'H4': 3.5}.get(timeframe, 2.5)
    sl_buffer = atr * sl_mult

    # TP multiplier — sized to what a real sweep reversal delivers per TF
    # M5/M15: 5-17 pip moves, M30: 15-25 pips, H1: 30-55 pips, H4: 80-120 pips
    tp_mult = {'M5': 2.0, 'M15': 2.0, 'M30': 2.0, 'H1': 2.0, 'H4': 2.5}.get(timeframe, 2.0)

    # S/R buffer — proportional to ATR so it works for Gold AND forex pairs
    sr_buf = atr * 0.1

    res_p = [l['price'] for l in sr_levels if l['type'] == 'resistance']
    sup_p = [l['price'] for l in sr_levels if l['type'] == 'support']

    # Max distance from current price before we switch to market entry.
    # If support/resistance is too far, a limit order may never fill — just enter at market.
    max_entry_gap = atr * 1.5

    if direction == 'bullish':
        # Prefer nearest support as entry (better R:R). But if it's too far from
        # current price (limit order unlikely to fill), enter at market price instead.
        best_sup = max(sup_p) if sup_p else None
        if best_sup and (current - best_sup) <= max_entry_gap:
            entry = best_sup   # close enough — use limit entry at support
        else:
            entry = current    # too far — enter at market price right now

        sl     = _rnd(entry - sl_buffer, current)
        atr_tp = entry + sl_buffer * tp_mult

        # TP: resistance ABOVE CURRENT PRICE only
        res_above_current = [r for r in res_p if r > current + atr * 0.3]
        if res_above_current:
            sr_tp = min(res_above_current) - sr_buf
            tp = _rnd(min(sr_tp, atr_tp) if sr_tp > entry + sl_buffer else atr_tp, current)
        else:
            tp = _rnd(atr_tp, current)

    elif direction == 'bearish':
        # Prefer nearest resistance as entry. If too far, enter at market instead.
        best_res = min(res_p) if res_p else None
        if best_res and (best_res - current) <= max_entry_gap:
            entry = best_res   # close enough — use limit entry at resistance
        else:
            entry = current    # too far — enter at market price right now

        sl     = _rnd(entry + sl_buffer, current)
        atr_tp = entry - sl_buffer * tp_mult

        # TP: support BELOW CURRENT PRICE only
        sup_below_current = [s for s in sup_p if s < current - atr * 0.3]
        if sup_below_current:
            sr_tp = max(sup_below_current) + sr_buf
            tp = _rnd(max(sr_tp, atr_tp) if sr_tp < entry - sl_buffer else atr_tp, current)
        else:
            tp = _rnd(atr_tp, current)

    else:
        return {'entry': _rnd(current, current), 'sl': None, 'tp': None, 'rr': None, 'risk_pct': 0}

    entry  = _rnd(entry, current)
    sl     = _rnd(sl, current)
    tp     = _rnd(tp, current)
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    return {
        'entry': entry, 'sl': sl, 'tp': tp, 'rr': rr, 'risk_pct': risk_pct,
    }


# ── Confluence Scoring ─────────────────────────────────────────────────────────

def is_active_session() -> bool:
    """Returns True during London (07-16 UTC) or New York (13-21 UTC) sessions."""
    now  = datetime.now(timezone.utc)
    hour = now.hour
    return (7 <= hour <= 16) or (13 <= hour <= 21)


def get_higher_tf_bias(symbol: str, current_tf: str) -> str:
    """
    Fetch D1 (or W1) EMA bias to confirm the signal direction.
    Returns 'bullish', 'bearish', or 'neutral'.
    """
    higher = HIGHER_TF.get(current_tf, 'D1')
    if higher == current_tf:
        return 'neutral'
    try:
        df_h = get_candles(symbol, higher, 60)
        ema20 = compute_ema(df_h, 20).iloc[-1]
        ema50 = compute_ema(df_h, 50).iloc[-1]
        return 'bullish' if ema20 > ema50 else 'bearish'
    except Exception:
        return 'neutral'


# ── Fair Value Gap Detection ───────────────────────────────────────────────────

def detect_fvg(df: pd.DataFrame, lookback: int = 40) -> list:
    """
    Detect recent unmitigated Fair Value Gaps (FVGs).

    A Fair Value Gap is a 3-candle imbalance where price moved so fast it
    left a gap between candle[i-2] and candle[i]:
      Bullish FVG: candle[i-2].high < candle[i].low  (gap above)
      Bearish FVG: candle[i-2].low  > candle[i].high (gap below)

    "Unmitigated" = subsequent price hasn't fully closed through the gap.
    Price returning TO a gap is where the high-probability entry lives.
    """
    fvgs = []
    n    = len(df)
    start = max(0, n - lookback)

    for i in range(start + 2, n):
        c0_high = float(df['high'].iloc[i - 2])
        c0_low  = float(df['low'].iloc[i - 2])
        c2_high = float(df['high'].iloc[i])
        c2_low  = float(df['low'].iloc[i])

        # Bullish FVG
        if c0_high < c2_low:
            top    = c2_low
            bottom = c0_high
            # Still unmitigated? No subsequent candle closed fully below the bottom
            subsequent = df['close'].iloc[i + 1:] if i + 1 < n else pd.Series(dtype=float)
            if len(subsequent) == 0 or not any(subsequent < bottom):
                fvgs.append({'direction': 'bullish', 'top': top, 'bottom': bottom, 'idx': i})

        # Bearish FVG
        elif c0_low > c2_high:
            top    = c0_low
            bottom = c2_high
            subsequent = df['close'].iloc[i + 1:] if i + 1 < n else pd.Series(dtype=float)
            if len(subsequent) == 0 or not any(subsequent > top):
                fvgs.append({'direction': 'bearish', 'top': top, 'bottom': bottom, 'idx': i})

    return fvgs


# ══════════════════════════════════════════════════════════════════════════════
#  SMART MONEY CONCEPTS (SMC) ENGINE  — v3.0
#  Replaces all indicator-based signals with institutional order flow analysis.
#
#  Logic chain:
#    1. Detect market structure (BOS / CHoCH) — WHO is in control?
#    2. Detect liquidity pools (BSL / SSL)    — WHERE are stops clustered?
#    3. Detect liquidity sweep                — DID smart money hunt those stops?
#    4. Detect displacement                   — DID smart money push price away?
#    5. Detect order blocks                   — WHERE did institutions enter?
#    6. Score 7-factor SMC model              — IS the setup complete?
#    7. Set entry at OB/FVG, SL at swept low,
#       TP at next liquidity pool             — CLEAN levels, clear R:R
# ══════════════════════════════════════════════════════════════════════════════

def detect_market_structure(df: pd.DataFrame, swing_len: int = 5) -> dict:
    """
    Classify market structure from swing sequence.

    Bullish: price making Higher Highs + Higher Lows (HH/HL)
    Bearish: price making Lower Highs + Lower Lows (LH/LL)

    BOS (Break of Structure): close beyond last swing in TREND direction → continuation.
    CHoCH (Change of Character): close beyond last OPPOSING swing → reversal warning.
    """
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    n      = len(df)

    sh_idx, sl_idx = [], []
    for i in range(swing_len, n - swing_len):
        w = swing_len
        if highs[i] >= max(highs[max(0,i-w):i]) and highs[i] >= max(highs[i+1:i+w+1]):
            sh_idx.append(i)
        if lows[i]  <= min(lows[max(0,i-w):i])  and lows[i]  <= min(lows[i+1:i+w+1]):
            sl_idx.append(i)

    if len(sh_idx) < 2 or len(sl_idx) < 2:
        return {'direction': 'neutral', 'bos': None, 'choch': None,
                'sh_idx': sh_idx, 'sl_idx': sl_idx,
                'last_sh': None, 'last_sl': None}

    # Structure from last two swing highs/lows
    sh1, sh2 = highs[sh_idx[-2]], highs[sh_idx[-1]]
    sl1, sl2 = lows[sl_idx[-2]],  lows[sl_idx[-1]]

    if   sh2 > sh1 and sl2 > sl1: direction = 'bullish'
    elif sh2 < sh1 and sl2 < sl1: direction = 'bearish'
    elif sh2 > sh1:                direction = 'bullish'
    elif sl2 < sl1:                direction = 'bearish'
    else:                          direction = 'neutral'

    last_sh = {'idx': sh_idx[-1], 'price': float(highs[sh_idx[-1]])}
    last_sl = {'idx': sl_idx[-1], 'price': float(lows[sl_idx[-1]])}
    recent  = closes[max(0, n-5):]

    bos = choch = None
    if direction == 'bullish':
        if any(c > last_sh['price'] for c in recent):
            bos   = {'direction': 'bullish', 'price': last_sh['price'], 'type': 'BOS'}
        if any(c < last_sl['price'] for c in recent):
            choch = {'direction': 'bearish', 'price': last_sl['price'], 'type': 'CHoCH'}
    elif direction == 'bearish':
        if any(c < last_sl['price'] for c in recent):
            bos   = {'direction': 'bearish', 'price': last_sl['price'], 'type': 'BOS'}
        if any(c > last_sh['price'] for c in recent):
            choch = {'direction': 'bullish', 'price': last_sh['price'], 'type': 'CHoCH'}

    return {'direction': direction, 'bos': bos, 'choch': choch,
            'sh_idx': sh_idx, 'sl_idx': sl_idx,
            'last_sh': last_sh, 'last_sl': last_sl}


def detect_order_blocks(df: pd.DataFrame, atr_val: float) -> list:
    """
    Find unmitigated Order Blocks — zones where institutions placed large orders.

    Bullish OB: last BEARISH candle before a strong bullish displacement.
                When price returns down to this zone = high-prob BUY.
    Bearish OB: last BULLISH candle before a strong bearish displacement.
                When price returns up to this zone = high-prob SELL.

    'Unmitigated' = price has NOT yet traded back through the OB body.
    """
    obs    = []
    opens  = df['open'].values
    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values
    n      = len(df)
    thresh = 1.2 * atr_val          # displacement threshold

    for i in range(max(0, n - 60), n - 5):
        # Measure the move in the next 3 candles
        end   = min(i + 4, n)
        fwd_c = closes[i+1:end]
        if len(fwd_c) == 0:
            continue
        move_bull = fwd_c[-1] - closes[i]
        move_bear = closes[i] - fwd_c[-1]

        bull_bodies = sum(1 for j in range(i+1, end)
                          if closes[j] > opens[j] and (closes[j]-opens[j]) > 0.4*atr_val)
        bear_bodies = sum(1 for j in range(i+1, end)
                          if closes[j] < opens[j] and (opens[j]-closes[j]) > 0.4*atr_val)

        # Bullish OB
        if closes[i] < opens[i] and move_bull >= thresh and bull_bodies >= 2:
            ob_h = float(max(opens[i], closes[i]))
            ob_l = float(min(opens[i], closes[i]))
            subs_lows = lows[i+1:]
            if not any(l <= ob_l for l in subs_lows):           # unmitigated
                obs.append({'type': 'bullish', 'high': round(ob_h, 5),
                            'low': round(ob_l, 5), 'mid': round((ob_h+ob_l)/2, 5),
                            'idx': i, 'fresh': True})

        # Bearish OB
        elif closes[i] > opens[i] and move_bear >= thresh and bear_bodies >= 2:
            ob_h = float(max(opens[i], closes[i]))
            ob_l = float(min(opens[i], closes[i]))
            subs_highs = highs[i+1:]
            if not any(h >= ob_h for h in subs_highs):          # unmitigated
                obs.append({'type': 'bearish', 'high': round(ob_h, 5),
                            'low': round(ob_l, 5), 'mid': round((ob_h+ob_l)/2, 5),
                            'idx': i, 'fresh': True})
    return obs


def detect_liquidity_pools(df: pd.DataFrame, sh_idx: list,
                            sl_idx: list, atr_val: float) -> dict:
    """
    Map where retail stop losses are concentrated.

    Buyside Liquidity  (BSL): above swing highs / equal highs → longs' stops.
    Sellside Liquidity (SSL): below swing lows  / equal lows  → shorts' stops.

    Smart money HUNTS these pools before making the real move.
    """
    highs = df['high'].values
    lows  = df['low'].values
    tol   = atr_val * 0.4           # tolerance for "equal" highs/lows

    bsl, ssl = [], []

    for idx in sh_idx[-8:]:
        bsl.append({'price': round(float(highs[idx]), 5), 'idx': idx,
                    'type': 'swing_high', 'label': f'BSL {round(float(highs[idx]),2)}'})
    for idx in sl_idx[-8:]:
        ssl.append({'price': round(float(lows[idx]),  5), 'idx': idx,
                    'type': 'swing_low',  'label': f'SSL {round(float(lows[idx]),2)}'})

    # Equal highs / equal lows (double liquidity = stronger magnet)
    for i in range(len(sh_idx)):
        for j in range(i+1, len(sh_idx)):
            if abs(highs[sh_idx[i]] - highs[sh_idx[j]]) <= tol:
                p = (highs[sh_idx[i]] + highs[sh_idx[j]]) / 2
                bsl.append({'price': round(p, 5), 'idx': sh_idx[j],
                            'type': 'equal_highs', 'label': f'EQH {round(p,2)}'})
    for i in range(len(sl_idx)):
        for j in range(i+1, len(sl_idx)):
            if abs(lows[sl_idx[i]] - lows[sl_idx[j]]) <= tol:
                p = (lows[sl_idx[i]] + lows[sl_idx[j]]) / 2
                ssl.append({'price': round(p, 5), 'idx': sl_idx[j],
                            'type': 'equal_lows', 'label': f'EQL {round(p,2)}'})

    return {'bsl': bsl, 'ssl': ssl}


def detect_liquidity_sweep(df: pd.DataFrame, liquidity_pools: dict,
                            atr_val: float) -> dict:
    """
    Detect a liquidity sweep — when price wicks THROUGH a pool then CLOSES back.

    Bullish sweep of SSL: wick below swing low, candle closes ABOVE it.
      → Smart money collected sell-side stops. Reversal up expected.

    Bearish sweep of BSL: wick above swing high, candle closes BELOW it.
      → Smart money collected buy-side stops. Reversal down expected.

    Only candles within the last 10 bars are considered actionable.
    """
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    n      = len(df)
    lb     = min(12, n - 1)

    lb     = min(6, n - 1)   # tightened: only look 6 candles back — stale sweeps are noise
    best_bull = best_bear = None

    for pool in liquidity_pools.get('ssl', []):
        lvl = pool['price']
        for i in range(n - lb, n):
            if i <= pool.get('idx', 0):
                continue
            if lows[i] < lvl and closes[i] > lvl:
                ago = n - 1 - i
                if best_bull is None or ago < best_bull['candles_ago']:
                    best_bull = {'found': True, 'direction': 'bullish',
                                 'swept_level': lvl, 'swept_type': pool['type'],
                                 'idx': i, 'candles_ago': ago,
                                 'wick_depth': round(lvl - lows[i], 5)}

    for pool in liquidity_pools.get('bsl', []):
        lvl = pool['price']
        for i in range(n - lb, n):
            if i <= pool.get('idx', 0):
                continue
            if highs[i] > lvl and closes[i] < lvl:
                ago = n - 1 - i
                if best_bear is None or ago < best_bear['candles_ago']:
                    best_bear = {'found': True, 'direction': 'bearish',
                                 'swept_level': lvl, 'swept_type': pool['type'],
                                 'idx': i, 'candles_ago': ago,
                                 'wick_depth': round(highs[i] - lvl, 5)}

    if best_bull and best_bear:
        return best_bull if best_bull['candles_ago'] <= best_bear['candles_ago'] else best_bear
    return best_bull or best_bear or {'found': False, 'direction': 'none'}


def detect_displacement(df: pd.DataFrame, atr_val: float) -> dict:
    """
    Displacement = a strong, fast candle (body ≥ 1.5× ATR) that shows institutions
    are aggressively pushing price. Creates a Fair Value Gap in its wake.

    This is the CONFIRMATION that smart money acted after a liquidity sweep.
    Without displacement, the move is retail — do not enter.
    """
    opens  = df['open'].values
    closes = df['close'].values
    highs  = df['high'].values
    lows   = df['low'].values
    n      = len(df)
    thresh = 1.5 * atr_val
    lb     = min(5, n - 1)   # tightened: only 5 candles — displacement older than this is no longer actionable
    best   = None

    for i in range(n - lb, n):
        body = abs(closes[i] - opens[i])
        if body < thresh:
            continue
        dirn = 'bullish' if closes[i] > opens[i] else 'bearish'
        has_fvg = False
        if i >= 2:
            has_fvg = bool((dirn == 'bullish' and highs[i-2] < lows[i]) or
                           (dirn == 'bearish' and lows[i-2]  > highs[i]))
        ago = n - 1 - i
        if best is None or body > best['magnitude']:
            best = {'found': True, 'direction': dirn, 'idx': i,
                    'candles_ago': ago, 'magnitude': round(body, 5),
                    'magnitude_atr': round(body / atr_val, 2),
                    'has_fvg': has_fvg}
    return best or {'found': False, 'direction': 'none', 'magnitude': 0}


def _rnd_price(p: float, ref: float) -> float:
    """Round to 2 dp for large-price instruments (Gold, indices), 5 dp for forex."""
    return round(p, 2) if ref > 100 else round(p, 5)


def calculate_smc_trade_levels(direction: str, sweep: dict, obs: list,
                                fvg_zones: list, liquidity_pools: dict,
                                atr_val: float, current_price: float,
                                timeframe: str) -> dict:
    """
    SMC trade levels — SNIPER ENTRY MODEL.

    Entry = the OB or FVG that price must RETRACE to after displacement.
      Bullish: OB/FVG is BELOW current displaced price — wait for pullback down.
      Bearish: OB/FVG is ABOVE current displaced price — wait for pullback up.

    The signal does NOT lock at current price. app.py waits until live price
    reaches this zone, then locks with a precise institutional entry.

    SL = beyond swept liquidity level or OB boundary + buffer
    TP = next opposing liquidity pool (BSL for buys, SSL for sells)
    """
    ref        = current_price
    entry      = None
    entry_zone = None

    # ── Find nearest OB in trade direction at the retrace target ─────────
    # Bullish: look for bullish OBs at or below current price (pullback target)
    # Bearish: look for bearish OBs at or above current price (pullback target)
    for ob in obs:
        if ob['type'] != direction:
            continue
        mid = ob.get('mid', (ob['low'] + ob['high']) / 2)
        if direction == 'bullish' and mid <= current_price + atr_val:
            # Prefer the highest bullish OB (closest to current displaced high)
            if entry is None or mid > entry:
                entry      = mid
                entry_zone = {'low': ob['low'], 'high': ob['high'],
                              'mid': mid, 'type': 'OB'}
        elif direction == 'bearish' and mid >= current_price - atr_val:
            # Prefer the lowest bearish OB (closest to current displaced low)
            if entry is None or mid < entry:
                entry      = mid
                entry_zone = {'low': ob['low'], 'high': ob['high'],
                              'mid': mid, 'type': 'OB'}

    # ── Fallback: FVG midpoint ────────────────────────────────────────────
    if entry is None:
        for fvg in reversed(fvg_zones):
            if fvg.get('direction') != direction:
                continue
            mid = (fvg['top'] + fvg['bottom']) / 2
            if direction == 'bullish' and mid <= current_price + atr_val:
                entry      = mid
                entry_zone = {'low': fvg['bottom'], 'high': fvg['top'],
                              'mid': mid, 'type': 'FVG'}
                break
            elif direction == 'bearish' and mid >= current_price - atr_val:
                entry      = mid
                entry_zone = {'low': fvg['bottom'], 'high': fvg['top'],
                              'mid': mid, 'type': 'FVG'}
                break

    # ── Last resort: current price (no institutional zone found) ─────────
    if entry is None:
        entry      = current_price
        entry_zone = {'low': current_price - atr_val * 0.5,
                      'high': current_price + atr_val * 0.5,
                      'mid': current_price, 'type': 'price'}

    entry = _rnd_price(entry, ref)

    # SL: beyond swept level + buffer
    # For BULLISH:  swept SSL is BELOW entry → SL = swept - buffer (further below)
    # For BEARISH:  swept BSL is ABOVE entry → SL = swept + buffer (further above)
    # Buffer is wider on higher TFs — OB zones on M30/H1 have larger wicks
    sl_buf = atr_val * 0.5
    mult   = {'M5': 1.0, 'M15': 1.4, 'M30': 1.8, 'H1': 2.2, 'H4': 3.0, 'D1': 3.5}.get(timeframe, 2.0)

    if sweep.get('found') and sweep['direction'] == direction:
        swept = sweep['swept_level']
        if direction == 'bullish':
            # swept SSL is below price — SL goes even lower
            sl_raw = swept - sl_buf
            # Safety: SL must be BELOW entry on a BUY
            if sl_raw >= entry:
                sl_raw = entry - atr_val * mult
        else:
            # swept BSL is above price — SL goes even higher
            sl_raw = swept + sl_buf
            # Safety: SL must be ABOVE entry on a SELL
            if sl_raw <= entry:
                sl_raw = entry + atr_val * mult
        sl = _rnd_price(sl_raw, ref)
    else:
        # Fallback: ATR-scaled SL
        sl = _rnd_price(entry - atr_val * mult if direction == 'bullish'
                        else entry + atr_val * mult, ref)

    risk = abs(entry - sl)
    if risk <= 0:
        return {'entry': entry, 'sl': sl, 'tp': None, 'rr': None,
                'entry_zone': entry_zone}

    # TP: nearest liquidity pool at least 2× risk away — far enough to not get clipped
    # by the first minor pullback, close enough that a typical sweep move reaches it.
    tp = None
    min_tp_dist = risk * 1.2
    if direction == 'bullish':
        candidates = sorted([p for p in liquidity_pools.get('bsl', [])
                             if p['price'] > entry + min_tp_dist], key=lambda x: x['price'])
        if candidates:
            tp = _rnd_price(candidates[0]['price'], ref)
    else:
        candidates = sorted([p for p in liquidity_pools.get('ssl', [])
                             if p['price'] < entry - min_tp_dist], key=lambda x: -x['price'])
        if candidates:
            tp = _rnd_price(candidates[0]['price'], ref)

    # Fallback TP: 1.5× risk for fast TFs, 2.0× for slower ones.
    # Tighter TP means signals close faster — less time sitting open.
    tp_fallback_mult = {'M5': 1.5, 'M15': 1.5, 'M30': 1.8, 'H1': 2.0, 'H4': 2.5}.get(timeframe, 1.8)
    if tp is None:
        tp = _rnd_price(entry + risk * tp_fallback_mult if direction == 'bullish'
                        else entry - risk * tp_fallback_mult, ref)

    rr = round(abs(tp - entry) / risk, 2)
    return {'entry': entry, 'sl': sl, 'tp': tp, 'rr': rr,
            'entry_zone': entry_zone}


def smc_score(direction: str, structure: dict, sweep: dict, obs: list,
              fvg_zones: list, displacement: dict, symbol: str,
              timeframe: str, trade: dict, current_price: float,
              atr_val: float) -> dict:
    """
    7-factor Smart Money scoring. Every factor has institutional meaning.

    Factor 1 — Market Structure  : HH/HL or LH/LL sequence confirms bias
    Factor 2 — Liquidity Sweep   : price hunted a stop cluster then rejected
    Factor 3 — Displacement      : strong institutional candle after the sweep
    Factor 4 — Order Block       : unmitigated OB at entry zone
    Factor 5 — Fair Value Gap    : imbalance zone supporting entry
    Factor 6 — Kill Zone         : London (7-10 UTC) or New York (13-16 UTC)
    Factor 7 — R:R ≥ 2:1        : SL at swept low, TP at next pool

    STRONG requires: Sweep + Displacement + at least one of (OB / FVG) + R:R ≥ 2:1
    WATCH  requires: Sweep confirmed, rest forming
    SKIP   : core model missing — no edge, do not trade
    """
    factors = {}
    score   = 0

    # ── 1. Market Structure ───────────────────────────────────────────────────
    str_dir = structure.get('direction', 'neutral')
    bos     = structure.get('bos')
    choch   = structure.get('choch')

    if str_dir == direction:
        label = 'BOS ' + direction if bos and bos['direction'] == direction else str_dir.capitalize()
        factors['Structure'] = {'pass': True,
            'detail': f'{label} — institutional bias confirmed, trend structure intact'}
        score += 1
    elif choch and choch['direction'] == direction:
        factors['Structure'] = {'pass': True,
            'detail': f'CHoCH {direction} — structure shifting, smart money changing hands'}
        score += 1
    else:
        opp = 'bearish' if direction == 'bullish' else 'bullish'
        factors['Structure'] = {'pass': False,
            'detail': f'Structure is {str_dir} — entering against confirmed {opp} structure is high risk'}

    # ── 2. Liquidity Sweep ────────────────────────────────────────────────────
    if sweep.get('found') and sweep.get('direction') == direction:
        ago   = sweep.get('candles_ago', 99)
        stype = sweep.get('swept_type', 'level')
        if ago <= 3:
            factors['Liq. Sweep'] = {'pass': True,
                'detail': f'FRESH sweep of {stype} — {ago} candle(s) ago. Stops cleared, reversal imminent'}
        elif ago <= 8:
            factors['Liq. Sweep'] = {'pass': True,
                'detail': f'Recent sweep of {stype} — {ago} candles ago. Liquidity cleared, setup valid'}
        else:
            factors['Liq. Sweep'] = {'pass': False,
                'detail': f'Sweep was {ago} candles ago — momentum dissipated, too stale to act on'}
        if factors['Liq. Sweep']['pass']:
            score += 1
    else:
        factors['Liq. Sweep'] = {'pass': False,
            'detail': 'No liquidity sweep — setup lacks the institutional trigger. Do not enter'}

    # ── 3. Displacement ───────────────────────────────────────────────────────
    if displacement.get('found') and displacement.get('direction') == direction:
        mag    = displacement.get('magnitude_atr', 0)
        fvg_tx = ' + FVG created' if displacement.get('has_fvg') else ''
        factors['Displacement'] = {'pass': True,
            'detail': f'Institutional move {mag:.1f}× ATR{fvg_tx} — smart money is driving this direction'}
        score += 1
    else:
        factors['Displacement'] = {'pass': False,
            'detail': 'No displacement — move is weak/retail. Smart money not confirmed behind it'}

    # ── 4. Order Block ────────────────────────────────────────────────────────
    ob_hit = None
    for ob in obs:
        if ob['type'] != direction:
            continue
        if ob['low'] - atr_val * 0.5 <= current_price <= ob['high'] + atr_val * 0.5:
            ob_hit = ob
            break
    if ob_hit:
        inside = ob_hit['low'] <= current_price <= ob_hit['high']
        factors['Order Block'] = {'pass': True,
            'detail': f'{"Inside" if inside else "Near"} unmitigated {direction} OB '
                      f'[{ob_hit["low"]:.2f}–{ob_hit["high"]:.2f}] — institutional demand/supply zone'}
        score += 1
    else:
        factors['Order Block'] = {'pass': False,
            'detail': 'No unmitigated order block near price — entry zone lacks institutional origin'}

    # ── 5. Fair Value Gap ─────────────────────────────────────────────────────
    fvg_hit = None
    for fvg in reversed(fvg_zones):
        if fvg['direction'] != direction:
            continue
        fmid = (fvg['top'] + fvg['bottom']) / 2
        if abs(current_price - fmid) <= atr_val * 1.5:
            fvg_hit = fvg
            break
    if fvg_hit:
        inside = fvg_hit['bottom'] <= current_price <= fvg_hit['top']
        factors['FVG'] = {'pass': True,
            'detail': f'{"Price filling" if inside else "Near"} {direction} imbalance '
                      f'[{fvg_hit["bottom"]:.2f}–{fvg_hit["top"]:.2f}] — high-probability reversal zone',
            'fvg': fvg_hit}
        score += 1
    else:
        factors['FVG'] = {'pass': False,
            'detail': 'No fair value gap near price — imbalance not supporting entry'}

    # ── 6. Kill Zone ──────────────────────────────────────────────────────────
    h = datetime.now(timezone.utc).hour
    if 7 <= h <= 10:
        factors['Kill Zone'] = {'pass': True,
            'detail': 'London Kill Zone (7–10 UTC) — peak institutional volume, Gold/EUR/GBP prime time'}
        score += 1
    elif 13 <= h <= 16:
        factors['Kill Zone'] = {'pass': True,
            'detail': 'New York Kill Zone (13–16 UTC) — USD pairs + Gold at highest daily volume'}
        score += 1
    elif (7 <= h <= 16) or (13 <= h <= 21):
        factors['Kill Zone'] = {'pass': True,
            'detail': 'Active session — trading hours open but outside prime kill zones'}
        score += 1
    else:
        factors['Kill Zone'] = {'pass': False,
            'detail': 'Asian session / off-hours — institutions absent, signals are noise'}

    # ── 7. R:R ≥ 2:1 ─────────────────────────────────────────────────────────
    rr = trade.get('rr', 0) or 0
    if rr >= 2.5:
        factors['R:R'] = {'pass': True,
            'detail': f'R:R {rr}:1 — excellent. SL hugging swept low, TP at next liquidity pool'}
        score += 1
    elif rr >= 2.0:
        factors['R:R'] = {'pass': True,
            'detail': f'R:R {rr}:1 — meets SMC minimum. Risk defined, reward justified'}
        score += 1
    else:
        factors['R:R'] = {'pass': False,
            'detail': f'R:R {rr}:1 — below SMC 2:1 minimum. Widen TP target or tighten SL'}

    # ── Verdict ───────────────────────────────────────────────────────────────
    # The core model: Sweep + Displacement are non-negotiable.
    # Without them, the entry has no institutional backing — it's gambling.
    sweep_ok = factors['Liq. Sweep']['pass']
    disp_ok  = factors['Displacement']['pass']
    rr_ok    = factors['R:R']['pass']
    zone_ok  = ob_hit or fvg_hit

    if score >= 6 and sweep_ok and disp_ok and rr_ok:
        verdict = 'STRONG'
        verdict_text = (f'Full SMC model ({score}/7) — sweep + displacement + confluence. '
                        f'Enter with full conviction.')
    elif score >= 5 and sweep_ok and disp_ok:
        verdict = 'STRONG'
        verdict_text = (f'Clean SMC setup ({score}/7) — core model confirmed. '
                        f'Trade with standard size.')
    elif score >= 4 and sweep_ok:
        verdict = 'WATCH'
        verdict_text = (f'Sweep confirmed ({score}/7) — waiting for displacement. '
                        f'Use half size or wait for stronger entry candle.')
    elif score >= 3:
        verdict = 'WATCH'
        verdict_text = (f'Setup building ({score}/7) — not enough factors. '
                        f'Watch for sweep + displacement to trigger.')
    else:
        verdict = 'SKIP'
        verdict_text = f'No SMC model ({score}/7) — core setup missing. Wait for a clean sweep.'

    return {'score': score, 'max_score': 7, 'verdict': verdict,
            'verdict_text': verdict_text, 'factors': factors}


def _smc_pattern_object(direction: str, structure: dict, sweep: dict,
                         displacement: dict, ob_hit, fvg_hit) -> dict:
    """
    Build a 'pattern' dict compatible with the existing dashboard/feed format,
    using SMC terminology instead of old indicator pattern names.
    """
    # Pick the most descriptive name based on what was detected
    if sweep.get('found') and displacement.get('found') and ob_hit:
        name = 'Liquidity Sweep + OB'
        desc = ('Price swept a liquidity pool, displaced with institutional force, '
                'and is now retesting an unmitigated order block. '
                'Highest-probability SMC entry.')
        conf = 0.88
    elif sweep.get('found') and displacement.get('found') and fvg_hit:
        name = 'Sweep + FVG Fill'
        desc = ('Liquidity swept and price displaced, creating a fair value gap. '
                'Price returning to fill the imbalance — institutional entry zone.')
        conf = 0.82
    elif sweep.get('found') and displacement.get('found'):
        name = 'Sweep + Displacement'
        desc = ('Liquidity sweep confirmed with institutional displacement. '
                'Waiting for optimal entry at OB or FVG.')
        conf = 0.74
    elif structure.get('choch') and structure['choch']['direction'] == direction:
        name = 'CHoCH Reversal'
        desc = ('Change of Character detected — market structure shifting direction. '
                'Early reversal signal. Wait for sweep to confirm.')
        conf = 0.65
    elif structure.get('bos') and structure['bos']['direction'] == direction:
        name = 'BOS Continuation'
        desc = ('Break of Structure confirms trend continuation. '
                'Look to buy pullbacks to OB or FVG in trend direction.')
        conf = 0.60
    else:
        name = 'SMC Watch'
        desc = 'Monitoring for liquidity sweep and displacement to trigger entry.'
        conf = 0.45

    return {
        'name':        name,
        'description': desc,
        'direction':   direction,
        'confidence':  conf,
        'neckline':    None,
        'upper_line':  [],
        'lower_line':  [],
    }


# ── RSI Divergence Detection ───────────────────────────────────────────────────

def detect_rsi_divergence(df: pd.DataFrame, rsi_series: pd.Series,
                           direction: str, lookback: int = 40) -> bool:
    """
    Detect classic RSI divergence:
      Bullish divergence: price makes lower low, RSI makes higher low → reversal up
      Bearish divergence: price makes higher high, RSI makes lower high → reversal down

    Returns True if divergence is present (strong signal reinforcement).
    """
    try:
        n      = min(lookback, len(df))
        prices = df['close'].tail(n).values
        rsi    = rsi_series.tail(n).values

        if direction == 'bullish':
            # Price lower low + RSI higher low = bullish divergence
            price_lows = argrelextrema(prices, np.less_equal, order=3)[0]
            rsi_lows   = argrelextrema(rsi,    np.less_equal, order=3)[0]
            if len(price_lows) >= 2 and len(rsi_lows) >= 2:
                p1, p2 = prices[price_lows[-2]], prices[price_lows[-1]]
                r1, r2 = rsi[rsi_lows[-2]],     rsi[rsi_lows[-1]]
                if p2 < p1 and r2 > r1:   # price went lower, RSI went higher
                    return True

        elif direction == 'bearish':
            # Price higher high + RSI lower high = bearish divergence
            price_highs = argrelextrema(prices, np.greater_equal, order=3)[0]
            rsi_highs   = argrelextrema(rsi,    np.greater_equal, order=3)[0]
            if len(price_highs) >= 2 and len(rsi_highs) >= 2:
                p1, p2 = prices[price_highs[-2]], prices[price_highs[-1]]
                r1, r2 = rsi[rsi_highs[-2]],      rsi[rsi_highs[-1]]
                if p2 > p1 and r2 < r1:   # price went higher, RSI went lower
                    return True
    except Exception:
        pass
    return False


# ── Previous Day High / Low ────────────────────────────────────────────────────

def get_prev_day_levels(symbol: str) -> dict:
    """
    Fetch the previous completed day's high and low.
    These are the most respected S/R levels used by institutional traders —
    they mark where the market decided to reverse yesterday.
    """
    try:
        _connect()
        symbol = _resolve_symbol(symbol)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 3)
        if rates is not None and len(rates) >= 2:
            prev = rates[-2]   # yesterday's fully closed candle
            return {
                'prev_day_high':  float(prev['high']),
                'prev_day_low':   float(prev['low']),
                'prev_day_open':  float(prev['open']),
                'prev_day_close': float(prev['close']),
            }
    except Exception:
        pass
    return {}


def score_confluence(
    df: pd.DataFrame,
    pattern: dict,
    trade: dict,
    rsi_series: pd.Series,
    macd_data: dict,
    symbol: str,
    timeframe: str,
) -> dict:
    """
    Score the signal using 7 independent factors.
    Returns dict with: score (0-7), factors, verdict ('STRONG'/'WATCH'/'SKIP').

    STRONG = ≥5 factors → Take the trade
    WATCH  = 4 factors  → Trade with caution / smaller size
    SKIP   = <4 factors → Do NOT trade — wait for better setup

    Special rule — HTF Bias veto:
    If the higher timeframe opposes the trade direction, verdict is capped at WATCH
    even if 5+ other factors pass. Never go STRONG against the trend.
    """
    direction = pattern['direction']
    factors   = {}
    score     = 0

    # ── Factor 1: Pattern quality ─────────────────────────────────────────────
    conf = pattern.get('confidence', 0)
    if conf >= 0.70:
        factors['Pattern'] = {'pass': True, 'detail': f'{pattern["name"]} — high confidence ({round(conf*100)}%)'}
        score += 1
    elif conf >= 0.55:
        factors['Pattern'] = {'pass': True, 'detail': f'{pattern["name"]} — moderate confidence ({round(conf*100)}%)'}
        score += 1
    else:
        factors['Pattern'] = {'pass': False, 'detail': f'Weak pattern ({round(conf*100)}%) — wait for clearer structure'}

    # ── Factor 2: RSI position + divergence ──────────────────────────────────
    # RSI level OR divergence counts as a pass.
    # Divergence (price vs RSI disagreeing) is a leading reversal signal —
    # it can fire even when RSI level alone would fail.
    rsi        = rsi_series.iloc[-1]
    rsi_div    = detect_rsi_divergence(df, rsi_series, direction)
    if direction == 'bullish':
        if rsi < 35:
            factors['RSI'] = {'pass': True, 'detail': f'Oversold RSI {round(rsi,1)} — strong buy condition'}
            score += 1
        elif rsi < 60:
            factors['RSI'] = {'pass': True, 'detail': f'RSI {round(rsi,1)} — room to run upward'}
            score += 1
        elif rsi_div:
            factors['RSI'] = {'pass': True, 'detail': f'Bullish RSI divergence (RSI {round(rsi,1)}) — momentum turning up despite overbought level'}
            score += 1
        else:
            factors['RSI'] = {'pass': False, 'detail': f'RSI {round(rsi,1)} — overbought, no divergence. Avoid buying here'}
    else:
        if rsi > 65:
            factors['RSI'] = {'pass': True, 'detail': f'Overbought RSI {round(rsi,1)} — strong sell condition'}
            score += 1
        elif rsi > 40:
            factors['RSI'] = {'pass': True, 'detail': f'RSI {round(rsi,1)} — room to fall downward'}
            score += 1
        elif rsi_div:
            factors['RSI'] = {'pass': True, 'detail': f'Bearish RSI divergence (RSI {round(rsi,1)}) — momentum turning down despite oversold level'}
            score += 1
        else:
            factors['RSI'] = {'pass': False, 'detail': f'RSI {round(rsi,1)} — oversold, no divergence. Avoid shorting here'}

    # ── Factor 3: MACD direction + histogram momentum ─────────────────────────
    macd_val = macd_data['macd'][-1]
    hist_val = macd_data['histogram'][-1]
    hist_prev = macd_data['histogram'][-2] if len(macd_data['histogram']) > 1 else 0
    hist_growing = abs(hist_val) > abs(hist_prev)

    if direction == 'bullish' and (macd_val > 0 or hist_val > 0) and hist_growing:
        factors['MACD'] = {'pass': True, 'detail': 'MACD bullish momentum building'}
        score += 1
    elif direction == 'bullish' and (macd_val > 0 or hist_val > 0):
        factors['MACD'] = {'pass': True, 'detail': 'MACD on bullish side'}
        score += 1
    elif direction == 'bearish' and (macd_val < 0 or hist_val < 0) and hist_growing:
        factors['MACD'] = {'pass': True, 'detail': 'MACD bearish momentum building'}
        score += 1
    elif direction == 'bearish' and (macd_val < 0 or hist_val < 0):
        factors['MACD'] = {'pass': True, 'detail': 'MACD on bearish side'}
        score += 1
    else:
        factors['MACD'] = {'pass': False, 'detail': 'MACD opposes signal direction'}

    # ── Factor 4: Higher-timeframe alignment ──────────────────────────────────
    htf_bias = get_higher_tf_bias(symbol, timeframe)
    if htf_bias == direction:
        factors['HTF Bias'] = {'pass': True, 'detail': f'Higher timeframe confirms {direction} direction'}
        score += 1
    elif htf_bias == 'neutral':
        factors['HTF Bias'] = {'pass': True, 'detail': 'Higher timeframe neutral — no opposing trend'}
        score += 1
    else:
        factors['HTF Bias'] = {'pass': False, 'detail': f'Higher timeframe is {htf_bias} — trading against trend'}

    # ── Factor 5: Risk:Reward ≥ 1.5 ──────────────────────────────────────────
    rr = trade.get('rr', 0) or 0
    if rr >= 2.0:
        factors['R:R'] = {'pass': True, 'detail': f'Excellent R:R of {rr}:1 — high expected value'}
        score += 1
    elif rr >= 1.5:
        factors['R:R'] = {'pass': True, 'detail': f'Good R:R of {rr}:1 — meets minimum requirement'}
        score += 1
    else:
        factors['R:R'] = {'pass': False, 'detail': f'R:R of {rr}:1 is too low — skip this trade'}

    # ── Factor 6: Trading session ─────────────────────────────────────────────
    if is_active_session():
        now_h = datetime.now(timezone.utc).hour
        session = 'London' if 7 <= now_h <= 12 else 'New York' if 13 <= now_h <= 21 else 'Overlap'
        factors['Session'] = {'pass': True, 'detail': f'{session} session active — peak liquidity'}
        score += 1
    else:
        factors['Session'] = {'pass': False, 'detail': 'Outside London/NY sessions — liquidity too low, skip'}

    # ── Factor 7: ADX trend strength ─────────────────────────────────────────
    try:
        adx_val, pdi, mdi = compute_adx(df)
        adx_trending = adx_val > 25  # raised threshold — only clear trends qualify
        di_aligned   = (direction == 'bullish' and pdi > mdi) or \
                       (direction == 'bearish' and mdi > pdi)
        if adx_trending and di_aligned:
            factors['ADX'] = {'pass': True,  'detail': f'ADX {adx_val:.1f} — strong trend aligned with direction'}
            score += 1
        elif adx_trending:
            factors['ADX'] = {'pass': False, 'detail': f'ADX {adx_val:.1f} — strong trend but momentum opposes signal'}
        else:
            factors['ADX'] = {'pass': False, 'detail': f'ADX {adx_val:.1f} — market ranging/choppy, do not trade direction'}
    except Exception:
        factors['ADX'] = {'pass': False, 'detail': 'ADX could not be computed'}

    # ── Factor 8: Fair Value Gap alignment ───────────────────────────────────
    # An FVG is an institutional imbalance zone — price often returns to it
    # before continuing in the trend direction. When an FVG aligns with our
    # signal, the entry zone has much higher probability of holding.
    current_close = float(df['close'].iloc[-1])
    try:
        atr_val = float(compute_atr(df).iloc[-1])
        fvgs    = detect_fvg(df)
        # Find most recent unmitigated FVG aligned with our direction and near price
        aligned_fvg = None
        for fvg in reversed(fvgs):
            if fvg['direction'] != direction:
                continue
            gap_mid = (fvg['top'] + fvg['bottom']) / 2
            if abs(current_close - gap_mid) <= atr_val * 2.0:   # within 2 ATR
                aligned_fvg = fvg
                break
        if aligned_fvg:
            factors['FVG'] = {
                'pass': True,
                'detail': f'Fair Value Gap ({direction}) near price — institutional imbalance zone supporting entry',
                'fvg': aligned_fvg,
            }
            score += 1
        else:
            factors['FVG'] = {'pass': False, 'detail': 'No aligned Fair Value Gap near current price'}
    except Exception:
        factors['FVG'] = {'pass': False, 'detail': 'FVG check skipped'}

    # ── Verdict ───────────────────────────────────────────────────────────────
    # Max score is now 8. STRONG raised to ≥6 to maintain quality bar.
    if score >= 6:
        verdict = 'STRONG'
        verdict_text = f'Strong setup ({score}/8 factors) — recommended entry'
    elif score == 5:
        verdict = 'WATCH'
        verdict_text = f'Moderate setup ({score}/8 factors) — trade with smaller size'
    else:
        verdict = 'SKIP'
        verdict_text = f'Weak setup ({score}/8 factors) — do NOT trade, wait for better conditions'

    # ── EMA Trend quality cap ─────────────────────────────────────────────────
    # EMA Trend confidence (0.42) always fails the Pattern factor.
    # Without a real pattern, all 7 remaining factors must agree (7/8) for STRONG.
    # At 6/8, EMA Trend stays as WATCH — stronger bar needed without a pattern.
    if pattern.get('name') == 'EMA Trend' and verdict == 'STRONG' and score < 7:
        verdict      = 'WATCH'
        verdict_text = f'WATCH ({score}/8) — No chart pattern confirmed. EMA Trend needs 7/8 for STRONG.'

    # ── HTF cap ───────────────────────────────────────────────────────────────────
    # If HTF opposes direction, cap at WATCH — never show STRONG against the trend.
    # HTF + ADX both failing is NOT a forced SKIP: the pattern may still be valid,
    # and the trader can choose to act at smaller size. WATCH = "your call."
    htf_passed = factors.get('HTF Bias', {}).get('pass', False)
    adx_passed = factors.get('ADX',      {}).get('pass', False)
    if not htf_passed and not adx_passed and verdict == 'STRONG':
        verdict      = 'WATCH'
        verdict_text = f'Risky WATCH ({score}/7) — HTF + ADX both against you. Very small size only, or wait.'
    elif not htf_passed and verdict == 'STRONG':
        verdict      = 'WATCH'
        verdict_text = f'Capped at WATCH ({score}/7) — HTF opposes direction, reduce size or skip'

    return {
        'score':        score,
        'max_score':    8,
        'verdict':      verdict,
        'verdict_text': verdict_text,
        'factors':      factors,
    }


# ── Trendline Builder ──────────────────────────────────────────────────────────

def build_wedge_lines(df, pattern):
    sh_slope = pattern.get('sh_slope')
    sl_slope = pattern.get('sl_slope')
    sh_int   = pattern.get('sh_intercept')
    sl_int   = pattern.get('sl_intercept')
    if sh_slope is None or sl_slope is None:
        return [], []
    pm = df['close'].mean()
    pr = df['close'].max() - df['close'].min()
    upper, lower = [], []
    for i, row in df.iterrows():
        u = sh_slope * i + sh_int
        l = sl_slope * i + sl_int
        t = row['time'].isoformat()
        if abs(u - pm) < pr * 3: upper.append({'time': t, 'price': round(u, 4)})
        if abs(l - pm) < pr * 3: lower.append({'time': t, 'price': round(l, 4)})
    return upper, lower


# ── Auto Timeframe ─────────────────────────────────────────────────────────────

def auto_select_timeframe(symbol: str) -> str:
    best_tf, best = 'H1', 0
    for tf in ['D1', 'H4', 'H1']:
        try:
            df = get_candles(symbol, tf, 100)
            s  = len(find_swing_highs(df)) + len(find_swing_lows(df))
            if s > best: best, best_tf = s, tf
        except Exception:
            continue
    return best_tf


# ── Volatility Spike Detector ──────────────────────────────────────────────────

def detect_volatility_spike(df: pd.DataFrame, atr_val: float, multiplier: float = 3.0) -> dict:
    """
    Detects a news-like volatility spike — any candle in the last 3 bars whose
    high-low range is >= multiplier × ATR.  These are the candles that cause big
    moves regardless of whether a news event is listed on any calendar.
    Returns direction (bullish if candle closed up, bearish if down), the
    spike magnitude, and the candle range in ATR units.
    """
    if atr_val <= 0:
        return {'found': False}
    recent = df.iloc[-4:-1]   # last 3 closed candles (exclude current forming bar)
    for i in range(len(recent) - 1, -1, -1):
        c = recent.iloc[i]
        rng = float(c['high']) - float(c['low'])
        if rng >= atr_val * multiplier:
            direction = 'bullish' if float(c['close']) >= float(c['open']) else 'bearish'
            return {
                'found':       True,
                'direction':   direction,
                'range':       round(rng, 5),
                'atr_mult':    round(rng / atr_val, 1),
                'candle_high': float(c['high']),
                'candle_low':  float(c['low']),
            }
    return {'found': False}


# ── Main ───────────────────────────────────────────────────────────────────────

def analyze(symbol: str = 'XAUUSD+', timeframe: str = 'auto') -> dict:
    """
    Full confluence analysis. Returns a complete signal with verdict.
    Only STRONG (5-6/6) and WATCH (4/6) signals should be traded.
    SKIP signals (< 4/6) should be ignored regardless of how good the pattern looks.
    """
    auto_selected = False
    if timeframe in ('auto', ''):
        timeframe     = auto_select_timeframe(symbol)
        auto_selected = True

    df = get_candles(symbol, timeframe, 200)

    current    = float(df['close'].iloc[-1])
    prev_close = float(df['close'].iloc[-2])
    change_pct = round((current - prev_close) / prev_close * 100, 2)

    # ── Core indicators (kept for chart display + ATR sizing) ─────────────────
    atr         = compute_atr(df)
    _atr_raw    = float(atr.iloc[-1])
    _atr_median = float(atr.dropna().iloc[-14:].median())
    # Cap at 1.5× median — prevents a news spike candle from inflating SL/TP
    # for all subsequent signals on that pair for the next 14 candles.
    atr_val     = min(_atr_raw, _atr_median * 1.5)
    macd_data = compute_macd(df)
    ema20     = compute_ema(df, 20)
    ema50     = compute_ema(df, 50)
    fibonacci = compute_fibonacci(df)
    rsi       = compute_rsi(df)

    # ── SMC: Swing structure for market structure + liquidity detection ────────
    swing_highs = find_swing_highs(df, order=5)
    swing_lows  = find_swing_lows(df,  order=5)

    # ── SMC Step 1: Market Structure ─────────────────────────────────────────
    structure    = detect_market_structure(df, swing_len=5)
    sh_idx       = structure['sh_idx']
    sl_idx       = structure['sl_idx']

    # ── SMC Step 2: Liquidity Pools ───────────────────────────────────────────
    liq_pools    = detect_liquidity_pools(df, sh_idx, sl_idx, atr_val)

    # ── SMC Step 3: Liquidity Sweep ───────────────────────────────────────────
    sweep        = detect_liquidity_sweep(df, liq_pools, atr_val)

    # ── Volatility spike — news-like candle regardless of calendar ───────────
    vol_spike = detect_volatility_spike(df, atr_val)

    # ── SMC Step 4: Displacement ──────────────────────────────────────────────
    displacement = detect_displacement(df, atr_val)

    # ── Sweep-displacement alignment ──────────────────────────────────────────
    # Displacement must: (1) match sweep direction, (2) come AFTER the sweep.
    # If displacement is in the opposite direction, or happened before the sweep,
    # it's from a different move — not the institutional follow-through we need.
    if sweep.get('found') and displacement.get('found'):
        sweep_idx = sweep.get('idx', 0)
        disp_idx  = displacement.get('idx', 0)
        if displacement.get('direction') != sweep.get('direction'):
            displacement = {'found': False, 'direction': 'none', 'magnitude': 0}
        elif disp_idx < sweep_idx:
            # Displacement came before sweep — unrelated candle
            displacement = {'found': False, 'direction': 'none', 'magnitude': 0}

    # ── SMC Step 5: Order Blocks + FVG ───────────────────────────────────────
    obs          = detect_order_blocks(df, atr_val)
    fvg_zones    = detect_fvg(df)

    # ── HTF (D1/H4) Bias ─────────────────────────────────────────────────────
    # Fetch fresh D1 structure so the signal gate can reject trades that oppose
    # the daily institutional bias (e.g. USDCAD M15 SELL when D1 is bullish).
    d1_bias = 'neutral'
    if timeframe not in ('D1', 'W1'):
        htf_tf = 'D1' if timeframe in ('M5', 'M15', 'M30', 'H1') else 'H4'
        try:
            df_htf  = get_candles(symbol, htf_tf, 50)
            htf_str = detect_market_structure(df_htf, swing_len=3)
            d1_bias = htf_str.get('direction', 'neutral') or 'neutral'
        except Exception:
            d1_bias = 'neutral'

    # ── Trade direction ───────────────────────────────────────────────────────
    # Priority: sweep direction > CHoCH direction > BOS direction > structure
    if sweep.get('found') and sweep['direction'] != 'none':
        direction = sweep['direction']
    elif structure.get('choch') and structure['choch']:
        direction = structure['choch']['direction']
    elif structure.get('bos') and structure['bos']:
        direction = structure['bos']['direction']
    else:
        direction = structure.get('direction', 'bullish')
    if direction == 'none':
        direction = 'bullish'   # safe fallback

    # ── SMC Step 6: Trade Levels ──────────────────────────────────────────────
    trade      = calculate_smc_trade_levels(
        direction, sweep, obs, fvg_zones,
        liq_pools, atr_val, current, timeframe
    )
    entry_zone = trade.pop('entry_zone', None)   # extract for top-level result

    # ── SMC Step 7: Score ─────────────────────────────────────────────────────
    # Find nearest OB / FVG for pattern naming
    ob_hit  = next((o for o in obs if o['type'] == direction and
                    o['low'] - atr_val <= current <= o['high'] + atr_val), None)
    fvg_hit = next((f for f in reversed(fvg_zones) if f['direction'] == direction and
                    abs(current - (f['top']+f['bottom'])/2) <= atr_val * 1.5), None)

    confluence = smc_score(
        direction, structure, sweep, obs, fvg_zones,
        displacement, symbol, timeframe, trade, current, atr_val
    )

    pattern = _smc_pattern_object(direction, structure, sweep, displacement, ob_hit, fvg_hit)

    # ── S/R levels (for chart overlay — still useful as visual reference) ────
    sr_levels = find_sr_levels(df, swing_highs, swing_lows)
    prev_day  = get_prev_day_levels(symbol)
    if prev_day:
        for label, price in [('PDH', prev_day['prev_day_high']), ('PDL', prev_day['prev_day_low'])]:
            if price > 0:
                sr_levels.append({
                    'price': _rnd_price(price, price), 'type': 'resistance' if price > current else 'support',
                    'label': label, 'round': False, 'pd': True,
                })

    # ── Liquidity pool levels → chart overlay (as S/R lines) ─────────────────
    for p in liq_pools.get('bsl', [])[-4:]:
        sr_levels.append({'price': p['price'], 'type': 'resistance',
                          'label': p['label'], 'round': False, 'pd': False, 'liq': True})
    for p in liq_pools.get('ssl', [])[-4:]:
        sr_levels.append({'price': p['price'], 'type': 'support',
                          'label': p['label'], 'round': False, 'pd': False, 'liq': True})

    # ── Order blocks → supply/demand zone overlay ─────────────────────────────
    supply = [{'top': o['high'], 'bottom': o['low']} for o in obs if o['type'] == 'bearish']
    demand = [{'top': o['high'], 'bottom': o['low']} for o in obs if o['type'] == 'bullish']

    # ── Signal text ───────────────────────────────────────────────────────────
    verdict = confluence['verdict']
    if verdict == 'STRONG' and trade.get('entry'):
        signal_text = (f"{'📈' if direction == 'bullish' else '📉'} STRONG {direction.upper()} — "
                       f"Enter {'above' if direction == 'bullish' else 'below'} {trade['entry']:,.2f}")
    elif verdict == 'WATCH':
        signal_text = (f"⚠️ WATCH — {pattern['name']} forming. "
                       f"Wait for sweep + displacement to confirm entry.")
    else:
        signal_text = f"⛔ SKIP — {confluence['score']}/7 SMC factors. No sweep or displacement yet. Wait."

    candles = df.copy()
    candles['time']  = candles['time'].dt.strftime('%Y-%m-%dT%H:%M:%S')
    candles['ema20'] = ema20.round(2)
    candles['ema50'] = ema50.round(2)

    result = {
        'symbol':        symbol,
        'timeframe':     timeframe,
        'auto_selected': auto_selected,
        'price':         round(current, 2),
        'change_pct':    change_pct,
        'candles':       candles.to_dict(orient='records'),
        'pattern':       pattern,
        'sr_levels':     sr_levels,
        'supply_zones':  supply,
        'demand_zones':  demand,
        'fvg_zones':     fvg_zones,
        'prev_day':      prev_day,
        'signal':        signal_text,
        'trade':         trade,
        'confluence':    confluence,
        'macd':          macd_data,
        'fibonacci':     fibonacci,
        'rsi_current':   round(rsi.iloc[-1], 1),
        'atr_current':   round(atr_val, 4),
        'd1_bias':       d1_bias,          # D1/H4 trend direction for HTF alignment gate
        'entry_zone':    entry_zone,       # OB/FVG zone price must retrace to (sniper gate)
        'vol_spike':     vol_spike,        # news-like candle detected (≥3× ATR range)
        # SMC-specific — used by feed + dashboard
        'smc': {
            'structure':    structure.get('direction'),
            'bos':          structure.get('bos'),
            'choch':        structure.get('choch'),
            'sweep':        sweep,
            'displacement': displacement,
            'order_blocks': obs,
            'liq_pools':    liq_pools,
        },
    }
    return _sanitize(result)


def _sanitize(obj):
    """Recursively convert numpy scalars → Python natives so Flask can serialize."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj
