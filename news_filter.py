"""
news_filter.py  v2
──────────────────
Two-layer news intelligence for AutoCycle.

Layer 1 — ForexFactory economic calendar (scheduled events)
  Source : https://nfs.faireconomy.media/ff_calendar_thisweek.json
  Cache  : 5 minutes
  Watch  : USD, EUR, GBP, JPY, CNY
  HIGH   → pause 30 min before release
  MEDIUM → pause 15 min before release
  After release → MONITORING phase (see below)

Layer 2 — ForexLive RSS (live breaking headlines, zero signup)
  Source : https://www.forexlive.com/feed/news (Reuters as backup)
  Cache  : 3 minutes
  Action : keyword sentiment + surprise detection
  Surprise → pause 10 min

MONITORING phase (smart post-news re-entry):
  - First 90 seconds after event: mandatory chaos window — never enter
  - After 90s: score the market every 20 seconds on 4 factors:
      1. Price moved $3+ in one clear direction (held, not spiked-reversed)
      2. Gravity is FIRE with score ≥ 4/5
      3. Headlines directional (BULLISH or BEARISH, not MIXED/NEUTRAL)
      4. Spread back to normal (≤ $1.00 for XAUUSD+)
  - 3 or 4 factors agree → allow entry (system enters WITH the news move)
  - Fewer → wait 20s and re-score
  - Hard cap: 10 min HIGH, 5 min MEDIUM → resume regardless after cap

Background awareness thread:
  - Runs every 2 minutes, independent of trading cycles
  - Updates _news_state so the whole system always has fresh awareness
  - Sends Telegram alert when a new event window opens during an active cycle

get_news_status() always returns a safe dict — never raises.
Fail-open on every error: trading never permanently blocked.
"""
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
    _ET_ZONE = ZoneInfo('America/New_York')
except Exception:
    _ET_ZONE = None

log = logging.getLogger('AutocycleBroker.NewsFilter')

# ── Currencies that move gold ─────────────────────────────────────────────────
WATCHED_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'CNY', 'XAU'}

# ── Pause windows (seconds) ───────────────────────────────────────────────────
PAUSE_PRE  = {'High': 1800, 'Medium': 900}    # before event
MONITOR_CAP = {'High': 600,  'Medium': 300}   # max monitoring window after event

CHAOS_WINDOW      = 90    # seconds after event: never enter regardless of score
MONITOR_INTERVAL  = 20    # seconds between score checks during monitoring
ENTRY_SCORE_MIN   = 3     # out of 4 factors needed to allow entry
SPREAD_LIMIT      = 1.00  # $1.00 max spread for XAUUSD+ (normal = $0.20-$0.50)
PRICE_MOVE_MIN    = 3.00  # $3+ price move needed in one direction

RSS_SURPRISE_PAUSE = 600  # 10 min pause on surprise/breaking headline

# ── Cache TTLs ────────────────────────────────────────────────────────────────
CALENDAR_TTL  = 300   # 5 minutes
HEADLINES_TTL = 180   # 3 minutes

# ── Data sources ──────────────────────────────────────────────────────────────
CALENDAR_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
RSS_URL      = 'https://www.forexlive.com/feed/news'
RSS_BACKUP   = 'https://feeds.reuters.com/reuters/businessNews'

# ── Gold-relevant keywords ────────────────────────────────────────────────────
_GOLD_RELEVANT = [
    'gold', 'xauusd', 'federal reserve', 'fed reserve', 'fomc', 'powell',
    'inflation', 'cpi', 'ppi', 'core cpi', 'nfp', 'non-farm', 'payroll',
    'gdp', 'treasury yield', 'dollar index', 'dxy', 'rate decision',
    'geopolit', 'war', 'conflict', 'ukraine', 'middle east', 'oil price',
    'recession', 'safe haven', 'risk off', 'risk-off',
]

_BULLISH_GOLD = [
    'gold rises', 'gold rallies', 'gold surges', 'gold climbs', 'gold gains',
    'gold up', 'gold hits high', 'gold reaches', 'gold at fresh',
    'dollar falls', 'dollar drops', 'dollar weakens', 'usd weakens',
    'dxy falls', 'dxy drops', 'yields fall', 'yields drop',
    'risk off', 'risk-off', 'safe haven demand', 'safe haven buying',
    'geopolit', 'conflict escalat', 'war escalat', 'tension rises',
    'recession fears', 'inflation surges', 'inflation rises', 'hot cpi',
    'fed dovish', 'rate cut', 'rate cuts', 'cut rates', 'stimulus',
    'quantitative easing', 'qe',
]

_BEARISH_GOLD = [
    'gold falls', 'gold drops', 'gold tumbles', 'gold slides', 'gold declines',
    'gold selling', 'gold pressure', 'gold lower', 'gold loses',
    'dollar rises', 'dollar gains', 'dollar strengthens', 'usd rises',
    'dxy rises', 'dxy gains', 'yields rise', 'yields surge',
    'risk on', 'risk-on', 'equities rise', 'stocks rally',
    'fed hawkish', 'rate hike', 'rate hikes', 'hike rates', 'tightening',
    'strong jobs', 'jobs beat', 'nfp beats', 'better than expected',
    'beats forecast', 'economy strong', 'gdp beats',
]

_SURPRISE = [
    'breaking:', 'breaking news', 'flash:', 'alert:', 'just in:',
    'emergency rate', 'surprise rate', 'surprise cut', 'surprise hike',
    'unexpected', 'shock', 'flash crash', 'circuit breaker',
    'central bank intervention', 'intervention',
    'sudden', 'massive spike', 'rapid move', 'explosive move',
]

# ── Internal caches ───────────────────────────────────────────────────────────
_cal_cache: dict        = {'data': [], 'ts': 0.0}
_rss_cache: dict        = {'data': [], 'ts': 0.0}
_rss_pause_until: float = 0.0

# Shared news state updated by background awareness thread
_news_state: dict = {
    'status'    : 'CLEAR',
    'reason'    : '',
    'event'     : None,
    'updated_at': 0.0,
}

_lock = threading.Lock()


# ── Time parsing ──────────────────────────────────────────────────────────────

def _event_ts(date_str: str, time_str: str) -> float | None:
    """Convert ForexFactory date + time (US Eastern) to UTC epoch."""
    if not time_str or time_str.lower() in ('all day', 'tentative', '', 'n/a'):
        return None
    try:
        dt_naive = datetime.strptime(
            f'{date_str} {time_str.strip().upper()}', '%Y-%m-%d %I:%M%p'
        )
        if _ET_ZONE:
            from zoneinfo import ZoneInfo as _ZI
            dt = dt_naive.replace(tzinfo=_ZI('America/New_York'))
        else:
            off = -4 if 3 <= dt_naive.month <= 11 else -5
            dt  = dt_naive.replace(tzinfo=timezone(timedelta(hours=off)))
        return dt.timestamp()
    except Exception as exc:
        log.debug(f'[News] Time parse failed "{date_str} {time_str}": {exc}')
        return None


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_calendar() -> list[dict]:
    """Fetch ForexFactory weekly calendar. Returns [] on error (fail-open)."""
    with _lock:
        if time.time() - _cal_cache['ts'] < CALENDAR_TTL:
            return list(_cal_cache['data'])
    try:
        r = requests.get(CALENDAR_URL, timeout=10)
        if r.ok:
            data = r.json()
            with _lock:
                _cal_cache['data'] = data
                _cal_cache['ts']   = time.time()
            return list(data)
    except Exception as exc:
        log.debug(f'[News] Calendar fetch error: {exc}')
    with _lock:
        return list(_cal_cache['data'])


def _fetch_headlines() -> list[tuple[str, float]]:
    """
    Fetch RSS headlines. Returns list of (title_lower, pub_timestamp).
    Falls back to Reuters if ForexLive is down.
    """
    with _lock:
        if time.time() - _rss_cache['ts'] < HEADLINES_TTL:
            return list(_rss_cache['data'])
    for url in (RSS_URL, RSS_BACKUP):
        try:
            r = requests.get(
                url, timeout=10,
                headers={'User-Agent': 'Mozilla/5.0 (AutoCycle NewsFilter)'}
            )
            if not r.ok:
                continue
            root  = ET.fromstring(r.content)
            items = []
            for item in root.iter('item'):
                title = (item.findtext('title') or '').lower()
                pub   = item.findtext('pubDate') or ''
                try:
                    from email.utils import parsedate_to_datetime
                    ts = parsedate_to_datetime(pub).timestamp()
                except Exception:
                    ts = time.time()
                items.append((title, ts))
            with _lock:
                _rss_cache['data'] = items
                _rss_cache['ts']   = time.time()
            return list(items)
        except Exception as exc:
            log.debug(f'[News] RSS fetch error ({url}): {exc}')
    with _lock:
        return list(_rss_cache['data'])


# ── Sentiment engine ──────────────────────────────────────────────────────────

def _score_headline(title: str) -> tuple[str, bool]:
    """Returns (sentiment, is_surprise). sentiment: BULLISH | BEARISH | NEUTRAL"""
    t           = title.lower()
    is_surprise = any(kw in t for kw in _SURPRISE)
    bull        = sum(1 for kw in _BULLISH_GOLD if kw in t)
    bear        = sum(1 for kw in _BEARISH_GOLD if kw in t)
    if bull > bear:
        return 'BULLISH', is_surprise
    if bear > bull:
        return 'BEARISH', is_surprise
    return 'NEUTRAL', is_surprise


def _analyze_recent(max_age: float = 600) -> dict:
    """
    Analyze gold-relevant headlines from the last max_age seconds.
    Returns {'sentiment', 'headlines', 'surprise'}.
    """
    now     = time.time()
    cutoff  = now - max_age
    sents   = []
    titles  = []
    surprise = False

    for title, ts in _fetch_headlines():
        if ts < cutoff:
            continue
        if not any(kw in title for kw in _GOLD_RELEVANT):
            continue
        sent, surp = _score_headline(title)
        sents.append(sent)
        titles.append(title)
        if surp:
            surprise = True

    bull = sents.count('BULLISH')
    bear = sents.count('BEARISH')

    if not sents:
        overall = 'NEUTRAL'
    elif bull > bear:
        overall = 'BULLISH'
    elif bear > bull:
        overall = 'BEARISH'
    elif bull == bear and bull > 0:
        overall = 'MIXED'
    else:
        overall = 'NEUTRAL'

    return {'sentiment': overall, 'headlines': titles[:5], 'surprise': surprise}


def get_headline_sentiment() -> str:
    """
    Public helper for engine's 4-factor scorer.
    Returns: 'BULLISH' | 'BEARISH' | 'MIXED' | 'NEUTRAL'
    """
    try:
        return _analyze_recent(max_age=300).get('sentiment', 'NEUTRAL')
    except Exception:
        return 'NEUTRAL'


# ── Calendar check ────────────────────────────────────────────────────────────

def _check_calendar(now: float) -> dict | None:
    """
    Return the most urgent active calendar event, or None if clear.
    'Active' = now is within the pre-window OR monitoring window.
    """
    best = None
    for ev in _fetch_calendar():
        country = ev.get('country', '').upper()
        impact  = ev.get('impact',  '')
        if country not in WATCHED_CURRENCIES:
            continue
        if impact not in ('High', 'Medium'):
            continue
        ts = _event_ts(ev.get('date', ''), ev.get('time', ''))
        if ts is None:
            continue

        pre_start   = ts - PAUSE_PRE[impact]
        monitor_end = ts + MONITOR_CAP[impact]

        if not (pre_start <= now <= monitor_end):
            continue

        ev_copy = dict(ev)
        ev_copy.update({
            '_ts'          : ts,
            '_monitor_end' : monitor_end,
            '_phase'       : 'PRE_NEWS' if now < ts else 'MONITORING',
            '_mins_to'     : round((ts - now) / 60),
            '_elapsed'     : max(0.0, now - ts),   # seconds since event fired
        })

        if best is None:
            best = ev_copy
        elif impact == 'High' and best.get('impact') != 'High':
            best = ev_copy
        elif (impact == best.get('impact') and
              abs(ts - now) < abs(best['_ts'] - now)):
            best = ev_copy

    return best


# ── RSS surprise check ────────────────────────────────────────────────────────

def _check_rss(now: float) -> dict | None:
    global _rss_pause_until
    with _lock:
        active_until = _rss_pause_until
    if now < active_until:
        mins_left = round((active_until - now) / 60)
        return {
            'status'    : 'PAUSED',
            'reason'    : f'Breaking headline detected — monitoring ({mins_left}min remaining)',
            'resume_at' : active_until,
            'event'     : None,
            'event_ts'  : None,
            'elapsed'   : 0.0,
        }
    analysis = _analyze_recent(max_age=600)
    if analysis['surprise']:
        resume = now + RSS_SURPRISE_PAUSE
        with _lock:
            _rss_pause_until = resume
        log.warning('[News] Breaking headline — trading paused 10min')
        return {
            'status'    : 'PAUSED',
            'reason'    : 'Breaking headline detected — monitoring',
            'resume_at' : resume,
            'event'     : None,
            'event_ts'  : now,
            'elapsed'   : 0.0,
        }
    return None


# ── Main API ──────────────────────────────────────────────────────────────────

def get_news_status() -> dict:
    """
    Called by engine before opening a cycle. Never raises — fail-open.

    Returns dict with keys:
      status      : 'CLEAR' | 'PRE_NEWS' | 'MONITORING' | 'PAUSED'
      reason      : human-readable string for Telegram / logs
      event_ts    : float — when the event fired (epoch), or None
      elapsed     : float — seconds since event fired (0 if PRE_NEWS)
      monitor_end : float — when monitoring window expires, or None
      sentiment   : 'BULLISH' | 'BEARISH' | 'MIXED' | 'NEUTRAL' | None
      headlines   : list[str]
      in_chaos    : bool — True if still in 90s chaos window
    """
    try:
        now = time.time()

        # ── Layer 1: calendar ─────────────────────────────────────────────
        cal = _check_calendar(now)
        if cal:
            phase   = cal['_phase']
            title   = cal.get('title',   'Event')
            country = cal.get('country', '?')
            impact  = cal.get('impact',  '?')
            elapsed = cal['_elapsed']
            in_chaos = (phase == 'MONITORING' and elapsed < CHAOS_WINDOW)

            if phase == 'PRE_NEWS':
                mins = cal['_mins_to']
                reason = (
                    f'{country} {title} in {mins}min ({impact} impact) — '
                    f'standing by'
                )
                sentiment = None
                headlines = []
            else:
                # MONITORING — read sentiment for scoring
                analysis  = _analyze_recent(max_age=900)
                sentiment = analysis['sentiment']
                headlines = analysis['headlines']
                secs_left = max(0, round(cal['_monitor_end'] - now))
                if in_chaos:
                    chaos_left = round(CHAOS_WINDOW - elapsed)
                    reason = (
                        f'Post-{title}: settling ({chaos_left}s chaos window) — '
                        f'scoring starts soon'
                    )
                else:
                    reason = (
                        f'Post-{title}: sentiment {sentiment} — '
                        f'{secs_left}s window remaining'
                    )

            return {
                'status'      : phase,
                'reason'      : reason,
                'event_ts'    : cal['_ts'],
                'elapsed'     : elapsed,
                'monitor_end' : cal['_monitor_end'],
                'event'       : {'title': title, 'country': country,
                                 'impact': impact, 'time': cal.get('time', '')},
                'sentiment'   : sentiment,
                'headlines'   : headlines,
                'in_chaos'    : in_chaos,
            }

        # ── Layer 2: RSS surprise ──────────────────────────────────────────
        rss = _check_rss(now)
        if rss:
            analysis          = _analyze_recent(max_age=600)
            rss['sentiment']  = analysis['sentiment']
            rss['headlines']  = analysis['headlines']
            rss['in_chaos']   = True   # always treat breaking news as chaotic initially
            rss['monitor_end'] = rss.get('resume_at')
            return rss

        # ── All clear ──────────────────────────────────────────────────────
        return {
            'status'      : 'CLEAR',
            'reason'      : '',
            'event_ts'    : None,
            'elapsed'     : 0.0,
            'monitor_end' : None,
            'event'       : None,
            'sentiment'   : None,
            'headlines'   : [],
            'in_chaos'    : False,
        }

    except Exception as exc:
        log.warning(f'[News] get_news_status fail-open: {exc}')
        return {
            'status': 'CLEAR', 'reason': '', 'event_ts': None,
            'elapsed': 0.0, 'monitor_end': None, 'event': None,
            'sentiment': None, 'headlines': [], 'in_chaos': False,
        }


def startup_check() -> dict:
    """Run on startup. Returns status + 'upcoming' events in next 30min."""
    try:
        status = get_news_status()
        if status['status'] == 'CLEAR':
            now = time.time()
            upcoming = []
            for ev in _fetch_calendar():
                if ev.get('impact') not in ('High', 'Medium'):
                    continue
                if ev.get('country', '').upper() not in WATCHED_CURRENCIES:
                    continue
                ts = _event_ts(ev.get('date', ''), ev.get('time', ''))
                if ts and 0 < (ts - now) <= 1800:
                    mins = round((ts - now) / 60)
                    upcoming.append(
                        f'{ev["country"]} {ev["title"]} in {mins}min ({ev["impact"]})'
                    )
            status['upcoming'] = upcoming
        else:
            status['upcoming'] = []
        return status
    except Exception as exc:
        log.warning(f'[News] startup_check error: {exc}')
        return {
            'status': 'CLEAR', 'reason': '', 'event_ts': None, 'elapsed': 0.0,
            'monitor_end': None, 'event': None, 'sentiment': None,
            'headlines': [], 'in_chaos': False, 'upcoming': [],
        }


# ── Background awareness thread ───────────────────────────────────────────────

def start_awareness_thread(tg_alert_fn=None):
    """
    Launch a background thread that polls news status every 2 minutes.
    tg_alert_fn: optional callable(msg) for Telegram alerts during active cycles.
    """
    def _loop():
        prev_status = 'CLEAR'
        while True:
            try:
                ns = get_news_status()
                cur = ns['status']

                with _lock:
                    _news_state['status']     = cur
                    _news_state['reason']     = ns['reason']
                    _news_state['event']      = ns['event']
                    _news_state['updated_at'] = time.time()

                # Alert on new PRE_NEWS or PAUSED transition
                if cur != prev_status:
                    if cur == 'PRE_NEWS' and tg_alert_fn:
                        tg_alert_fn(
                            f'📰 <b>Upcoming news event</b>\n'
                            f'{ns["reason"]}\n'
                            f'Trading will pause automatically.'
                        )
                    elif cur == 'PAUSED' and tg_alert_fn:
                        tg_alert_fn(
                            f'⚡ <b>Breaking news</b>\n'
                            f'{ns["reason"]}\n'
                            f'Trading paused 10min.'
                        )
                    log.info(f'[News] Awareness: {prev_status} → {cur}')
                    prev_status = cur

            except Exception as exc:
                log.debug(f'[News] Awareness thread error: {exc}')
            time.sleep(120)   # 2-minute poll

    t = threading.Thread(target=_loop, name='news-awareness', daemon=True)
    t.start()
    log.info('[News] Background awareness thread started')
