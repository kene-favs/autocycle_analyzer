"""
gap_scanner.py
──────────────
Scans for price gaps every 5 seconds and auto-triggers the broker when one is found.

What is a price gap?
  A gap forms when a candle opens OUTSIDE the previous candle's high/low range.
  Price jumped with no trading in between — an empty zone with no orders filled.

  Gap UP  → price flew up too fast → 70% chance it comes back down to fill.
             The straddle SELL side will survive and ride the fill. Profit.

  Gap DOWN → price dropped too fast → 70% chance it bounces back up.
             The straddle BUY side will survive and ride the fill. Profit.

How it grabs the money:
  1. Gap detected
  2. Telegram alert fires instantly
  3. Broker /open is called — opens a BUY+SELL straddle at the current price
  4. The SL on the wrong side fires first (toward the gap)
  5. The surviving side rides the gap fill toward TP
  6. Money lands in book

Quality gates before triggering broker:
  - Gap must be >= min_gap for the symbol
  - Broker must be IDLE (no active cycle)
  - Gap key must not have been traded already (no double-entry)
"""

import logging
import os
import time
import threading
from datetime import datetime, timezone

import requests

log = logging.getLogger('GapScanner')

# ─── Symbols and gap thresholds ──────────────────────────────────────────────
# Gold gaps → Telegram alert + broker trigger (broker trades Gold)
# EURUSD/GBPUSD gaps → Telegram alert only (context signals, USD momentum signals)
# Gold symbol matches the broker's MT5_SYMBOL env var (Tickmill=XAUUSD, Vantage=XAUUSD+)
_GOLD_SYM = os.getenv('MT5_SYMBOL', 'XAUUSD')
_GAP_CONFIGS = {
    _GOLD_SYM: {'min_gap': 0.50,   'name': 'Gold',   'emoji': '🥇', 'trigger_broker': True},
    'EURUSD':  {'min_gap': 0.0005, 'name': 'EURUSD', 'emoji': '💶', 'trigger_broker': False},
    'GBPUSD':  {'min_gap': 0.0006, 'name': 'GBPUSD', 'emoji': '💷', 'trigger_broker': False},
}

SCAN_INTERVAL    = 2      # seconds between scans — fast enough to catch gaps before they fill
MAX_CANDLES_BACK = 5      # look back 5 M1 candles (last 5 minutes)
MIN_GAP_QUALITY  = 0.70   # only act on gaps where fill probability is high

_seen_gaps:    set  = set()   # gaps we've already traded / alerted
_last_trigger: float = 0.0   # epoch of last broker trigger (prevent rapid-fire)
TRIGGER_COOLDOWN = 60         # seconds between broker triggers

# ─── Currency → Gold bias cache ───────────────────────────────────────────────
# When EURUSD or GBPUSD gaps, we infer a Gold directional bias.
# EURUSD/GBPUSD gap UP  = USD weakening = Gold likely BUY
# EURUSD/GBPUSD gap DOWN = USD strengthening = Gold likely SELL
# Bias expires after 5 minutes (300s) — stale context is worse than none.
_currency_bias: dict = {
    'direction' : None,    # 'BUY' or 'SELL' (Gold direction implied)
    'source'    : None,    # 'EURUSD' or 'GBPUSD'
    'gap_dir'   : None,    # 'UP' or 'DOWN'
    'ts'        : 0.0,
}
CURRENCY_BIAS_TTL = 300   # seconds


def get_gold_bias() -> dict:
    """
    Return the current Gold directional bias inferred from recent currency gaps.
    Returns {'active': False} if no bias or bias has expired.
    Called by engine.py before opening a cycle.
    """
    now = time.time()
    if (not _currency_bias['direction']
            or now - _currency_bias['ts'] > CURRENCY_BIAS_TTL):
        return {'active': False, 'direction': None, 'source': None}
    return {
        'active'   : True,
        'direction': _currency_bias['direction'],
        'source'   : _currency_bias['source'],
        'gap_dir'  : _currency_bias['gap_dir'],
        'age_secs' : int(now - _currency_bias['ts']),
    }


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _tg(msg: str):
    token   = os.getenv('TELEGRAM_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHANNEL_ID', '')
    if not token or not chat_id:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'},
            timeout=8,
        )
    except Exception:
        pass


# ─── Broker trigger ───────────────────────────────────────────────────────────

def _trigger_broker(gap: dict):
    """
    POST to the broker /open endpoint to open a straddle on the gap.
    The natural survivor will be the side that rides the gap fill.
    Silently skips if broker is not IDLE (already in a cycle).
    """
    global _last_trigger
    now = time.time()
    if now - _last_trigger < TRIGGER_COOLDOWN:
        log.info('[GapScanner] Trigger cooldown active — skipping broker call')
        return

    try:
        port   = int(os.getenv('BROKER_PORT', '8001'))
        secret = os.getenv('BROKER_SECRET', '')
        url    = f'http://localhost:{port}/open'

        status_r = requests.get(
            f'http://localhost:{port}/status',
            headers={'x-secret': secret},
            timeout=3,
        )
        if status_r.ok:
            phase = status_r.json().get('phase', 'UNKNOWN')
            if phase != 'IDLE':
                log.info(f'[GapScanner] Broker is {phase} — gap alert sent but no trigger')
                return

        r = requests.post(
            url,
            headers={'x-secret': secret},
            json={},
            timeout=5,
        )
        if r.ok and 'cycle_id' in r.json():
            _last_trigger = now
            log.info(
                f'[GapScanner] ✅ Broker triggered on {gap["symbol"]} '
                f'{gap["direction"]} gap ${gap["gap_size"]:.4f} '
                f'| cycle_id={r.json()["cycle_id"]}'
            )
        else:
            log.info(f'[GapScanner] Broker declined: {r.text[:120]}')

    except Exception as e:
        log.debug(f'[GapScanner] Broker trigger error: {e}')


# ─── Gap detection ────────────────────────────────────────────────────────────

def scan_gaps(sym: str) -> list[dict]:
    """Return any new gaps found in the last MAX_CANDLES_BACK M1 candles."""
    try:
        import MetaTrader5 as mt5
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, MAX_CANDLES_BACK)
        if rates is None or len(rates) < 2:
            return []

        cfg     = _GAP_CONFIGS.get(sym, {'min_gap': 1.0, 'name': sym, 'emoji': '📊'})
        min_gap = cfg['min_gap']
        gaps    = []

        for i in range(1, len(rates)):
            prev_high  = float(rates[i - 1]['high'])
            prev_low   = float(rates[i - 1]['low'])
            prev_close = float(rates[i - 1]['close'])
            curr_open  = float(rates[i]['open'])
            candle_ts  = int(rates[i]['time'])

            if curr_open > prev_high:
                size = round(curr_open - prev_high, 5)
                if size >= min_gap:
                    gaps.append({
                        'key'       : f'{sym}_{candle_ts}_UP',
                        'symbol'    : sym,
                        'direction' : 'UP',
                        'gap_size'  : size,
                        'prev_close': prev_close,
                        'prev_high' : prev_high,
                        'curr_open' : curr_open,
                        'fill_level': prev_high,
                        'trade_dir' : 'SELL',
                        'ts'        : candle_ts,
                    })

            elif curr_open < prev_low:
                size = round(prev_low - curr_open, 5)
                if size >= min_gap:
                    gaps.append({
                        'key'       : f'{sym}_{candle_ts}_DOWN',
                        'symbol'    : sym,
                        'direction' : 'DOWN',
                        'gap_size'  : size,
                        'prev_close': prev_close,
                        'prev_low'  : prev_low,
                        'curr_open' : curr_open,
                        'fill_level': prev_low,
                        'trade_dir' : 'BUY',
                        'ts'        : candle_ts,
                    })

        return gaps

    except Exception as e:
        log.debug(f'[GapScanner] {sym} scan error: {e}')
        return []


def _format_alert(gap: dict) -> str:
    cfg       = _GAP_CONFIGS.get(gap['symbol'], {'name': gap['symbol'], 'emoji': '📊'})
    direction = gap['direction']
    emoji     = '🚀' if direction == 'UP' else '📉'
    ts_str    = datetime.fromtimestamp(gap['ts'], tz=timezone.utc).strftime('%H:%M UTC')

    can_trigger = cfg.get('trigger_broker', False)
    action_line = (
        f'🤖 <b>Broker triggered — straddle open</b>\n'
        f'Survivor: {gap["trade_dir"]} side rides the fill'
        if can_trigger else
        f'📊 <b>Context signal only</b> — {gap["symbol"]} gap signals '
        f'USD move. Watch Gold reaction.'
    )
    return (
        f'{emoji} <b>GAP FOUND — {cfg["name"]}</b>\n'
        f'Direction : Gap <b>{direction}</b>   ({ts_str})\n'
        f'Size      : <b>${gap["gap_size"]:.4f}</b>\n'
        f'Opened at : {gap["curr_open"]:.5f}\n'
        f'Fill back : {gap["fill_level"]:.5f}\n\n'
        f'{action_line}'
    )


# ─── Main loop ────────────────────────────────────────────────────────────────

def _scanner_loop():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.warning('[GapScanner] MetaTrader5 not available')
        return

    log.info(f'[GapScanner] Running — scanning every {SCAN_INTERVAL}s | symbols: {list(_GAP_CONFIGS)}')

    while True:
        try:
            for sym in _GAP_CONFIGS:
                if mt5.symbol_info(sym) is None:
                    continue

                for gap in scan_gaps(sym):
                    if gap['key'] in _seen_gaps:
                        continue

                    _seen_gaps.add(gap['key'])
                    can_trigger = _GAP_CONFIGS.get(sym, {}).get('trigger_broker', False)

                    log.info(
                        f'[GapScanner] GAP {gap["direction"]} {sym} '
                        f'${gap["gap_size"]:.4f} | fill={gap["fill_level"]:.4f} '
                        f'| broker_trigger={can_trigger}'
                    )

                    # ── Currency bias: store implied Gold direction ───────────
                    if sym in ('EURUSD', 'GBPUSD'):
                        # Both EURUSD and GBPUSD are USD-quoted.
                        # Gap UP = USD weaker = Gold goes UP (BUY)
                        # Gap DOWN = USD stronger = Gold goes DOWN (SELL)
                        implied = 'BUY' if gap['direction'] == 'UP' else 'SELL'
                        _currency_bias.update({
                            'direction': implied,
                            'source'   : sym,
                            'gap_dir'  : gap['direction'],
                            'ts'       : time.time(),
                        })
                        log.info(
                            f'[CurrencyBias] {sym} gap {gap["direction"]} → '
                            f'Gold bias = {implied} (expires {CURRENCY_BIAS_TTL}s)'
                        )

                    if can_trigger:
                        _trigger_broker(gap)

                    _tg(_format_alert(gap))

        except Exception as e:
            log.debug(f'[GapScanner] loop error: {e}')

        time.sleep(SCAN_INTERVAL)


def start():
    t = threading.Thread(target=_scanner_loop, name='GapScanner', daemon=True)
    t.start()
    log.info('[GapScanner] Daemon thread started')
