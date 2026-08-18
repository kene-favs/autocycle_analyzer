"""
AutoCycle News Calendar
=======================
Fetches ALL economic events from Forex Factory's public feed.
Tiered handling:
  HIGH   — NFP, CPI, FOMC, GDP — blocks FIRE signals ±120s, then hunts post-news OB
  MEDIUM — ADP, ISM, Retail Sales — shows caution flag, WATCH only during window
  LOW    — minor releases — shows as background context only

Provides:
  - get_upcoming_events(tier)  → events by impact tier
  - get_next_event()           → single next HIGH-impact event
  - is_news_active(tier)       → True if within window of an event of that tier
  - get_active_event()         → event currently in its news window
  - is_post_news_window()      → True 90-300s AFTER high-impact = post-news OB window
  - is_comex_opex_week()       → True within 2 days of COMEX gold options expiry
  - get_gold_context()         → full context dict for gold signals

All times are returned in UTC.
"""

import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────

_FF_URL      = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
_CACHE_TTL   = 10 * 60     # refresh every 10 minutes

# News window per tier (seconds before/after event)
_NEWS_WINDOW = {
    'high':   120,   # ±2 min around HIGH impact = block new FIRE locks
    'medium':  60,   # ±1 min around MEDIUM = WATCH only, caution flag
    'low':     30,   # ±30s around LOW = context only, no blocking
}
_POST_NEWS_OB_WINDOW = (90, 300)   # 90-300s AFTER high-impact = hunt the post-news OB

# Pairs most affected by each country's news
_COUNTRY_PAIRS = {
    'USD': ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'XAUUSD'],
    'EUR': ['EURUSD', 'EURJPY', 'EURGBP'],
    'GBP': ['GBPUSD', 'GBPJPY', 'EURGBP'],
    'JPY': ['USDJPY', 'EURJPY', 'GBPJPY'],
    'AUD': ['AUDUSD'],
    'NZD': ['NZDUSD'],
    'CAD': ['USDCAD'],
    'CHF': ['USDCHF'],
    'XAU': ['XAUUSD'],
}

# ── Internal state ───────────────────────────────────────────────────────────────

_cache_lock   = threading.Lock()
_cached_events: list = []
_cache_ts: float     = 0.0


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _parse_event_time(date_str: str, time_str: str = '') -> Optional[datetime]:
    """
    Forex Factory changed its API format.
    NEW format: date = '2026-08-03T10:00:00-04:00' (full ISO 8601, no separate time field)
    OLD format: date = '07-28-2026', time = '8:30am' (separate fields)
    Handles both.
    """
    try:
        if not date_str:
            return None

        # NEW format — ISO 8601 datetime with timezone offset
        if 'T' in date_str:
            dt = datetime.fromisoformat(date_str)
            return dt.astimezone(timezone.utc)

        # OLD format — separate date + time strings
        if not time_str or time_str.strip() in ('', 'All Day', 'Tentative'):
            return None

        dt_str = f"{date_str} {time_str}"
        naive  = datetime.strptime(dt_str, '%m-%d-%Y %I:%M%p')

        month = naive.month
        if 3 < month < 11:
            offset = timedelta(hours=4)   # EDT
        elif month in (3, 11):
            offset = timedelta(hours=4) if month == 3 else timedelta(hours=5)
        else:
            offset = timedelta(hours=5)   # EST

        return naive.replace(tzinfo=timezone.utc) + offset

    except Exception:
        return None


def _fetch_events() -> list:
    """Download and parse this week's calendar. Returns list of event dicts."""
    try:
        resp = requests.get(_FF_URL, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        log.warning(f"NewsCalendar fetch failed: {exc}")
        return []

    events = []
    now    = datetime.now(timezone.utc)

    for item in raw:
        impact = (item.get('impact') or '').strip().lower()
        # Accept ALL impact levels — tier determines how each is handled
        if impact not in ('high', 'medium', 'low'):
            continue

        country  = (item.get('country') or '').upper()
        title    = item.get('title', 'Unknown Event')
        date_str = item.get('date', '')
        time_str = item.get('time', '')   # empty in new API format — handled in parser

        event_time = _parse_event_time(date_str, time_str)
        if event_time is None:
            continue

        # Only keep events in a reasonable window: past 24h → next 7 days
        age = (now - event_time).total_seconds()
        if age > 86400 or (event_time - now).total_seconds() > 7 * 86400:
            continue

        pairs = _COUNTRY_PAIRS.get(country, [])

        events.append({
            'title':    title,
            'country':  country,
            'impact':   impact,          # 'high' | 'medium' | 'low'
            'time_utc': event_time.isoformat(),
            'ts':       event_time.timestamp(),
            'forecast': item.get('forecast', ''),
            'previous': item.get('previous', ''),
            'actual':   item.get('actual', ''),
            'pairs':    pairs,
            'gold_relevant': 'XAUUSD' in pairs or country == 'USD',
        })

    # Sort chronologically
    events.sort(key=lambda e: e['ts'])
    high = sum(1 for e in events if e['impact'] == 'high')
    med  = sum(1 for e in events if e['impact'] == 'medium')
    low  = sum(1 for e in events if e['impact'] == 'low')
    log.info(f"NewsCalendar: loaded {len(events)} events ({high} HIGH / {med} MEDIUM / {low} LOW)")
    return events


def _refresh_if_needed():
    """Refresh cache if stale."""
    global _cached_events, _cache_ts
    with _cache_lock:
        if time.time() - _cache_ts > _CACHE_TTL:
            events = _fetch_events()
            _cached_events = events
            _cache_ts      = time.time()


# ── Public API ───────────────────────────────────────────────────────────────────

def get_upcoming_events(tier: str = 'all') -> list:
    """
    Returns events coming up in the next 24 hours plus last 10 minutes.
    tier: 'high' | 'medium' | 'low' | 'all'
    Each event includes `seconds_until` (negative = already fired).
    """
    _refresh_if_needed()
    now = time.time()
    result = []
    with _cache_lock:
        for ev in _cached_events:
            if tier != 'all' and ev['impact'] != tier:
                continue
            delta = ev['ts'] - now
            if -600 <= delta <= 86400:
                entry = dict(ev)
                entry['seconds_until'] = int(delta)
                result.append(entry)
    return result


def get_next_event(tier: str = 'high') -> Optional[dict]:
    """Returns the single next upcoming event of the given tier, or None."""
    _refresh_if_needed()
    now = time.time()
    with _cache_lock:
        for ev in _cached_events:
            if tier != 'all' and ev['impact'] != tier:
                continue
            if ev['ts'] > now:
                entry = dict(ev)
                entry['seconds_until'] = int(ev['ts'] - now)
                return entry
    return None


def is_news_active(tier: str = 'high') -> bool:
    """
    True if RIGHT NOW is within the news window of an event of the given tier.
    tier: 'high' (±120s) | 'medium' (±60s) | 'low' (±30s)
    HIGH  → block new FIRE locks
    MEDIUM → WATCH only / caution flag
    LOW   → context only
    """
    _refresh_if_needed()
    now    = time.time()
    window = _NEWS_WINDOW.get(tier, 120)
    with _cache_lock:
        for ev in _cached_events:
            if ev['impact'] != tier:
                continue
            if abs(ev['ts'] - now) <= window:
                return True
    return False


def get_active_event() -> Optional[dict]:
    """
    Returns the HIGH-impact event currently in its news window, or None.
    The 'phase' key tells you: 'imminent' (before), 'live' (within 30s), 'settling' (after).
    """
    _refresh_if_needed()
    now = time.time()
    with _cache_lock:
        for ev in _cached_events:
            delta = ev['ts'] - now    # positive = event is future
            window = _NEWS_WINDOW.get(ev.get('impact', 'high'), 120)
            if -window <= delta <= window:
                if delta > 30:
                    phase = 'imminent'
                elif delta >= -30:
                    phase = 'live'
                else:
                    phase = 'settling'
                entry = dict(ev)
                entry['seconds_until'] = int(delta)
                entry['phase'] = phase
                return entry
    return None


def events_fired_recently(since_seconds: int = 90) -> list:
    """
    Returns HIGH-impact events that fired in the last `since_seconds` seconds.
    Used by the news sniper to know when to activate.
    """
    _refresh_if_needed()
    now = time.time()
    result = []
    with _cache_lock:
        for ev in _cached_events:
            age = now - ev['ts']
            if 0 < age <= since_seconds:
                entry = dict(ev)
                entry['seconds_ago'] = int(age)
                result.append(entry)
    return result


def is_news_day() -> bool:
    """True if there is at least one HIGH-impact event today (UTC)."""
    _refresh_if_needed()
    today = datetime.now(timezone.utc).date()
    with _cache_lock:
        for ev in _cached_events:
            ev_date = datetime.fromtimestamp(ev['ts'], tz=timezone.utc).date()
            if ev_date == today:
                return True
    return False


def is_post_news_ob_window() -> bool:
    """
    True if we are 90-300 seconds AFTER a HIGH-impact event.
    This is the best window to hunt post-news OBs:
    - The chaos spike has settled (first 90s)
    - The real institutional move is forming its retrace OB
    - Price will come back to fill the OB before continuing
    Gold scalper should be AGGRESSIVE during this window.
    """
    _refresh_if_needed()
    now  = time.time()
    lo, hi = _POST_NEWS_OB_WINDOW
    with _cache_lock:
        for ev in _cached_events:
            if ev['impact'] != 'high':
                continue
            age = now - ev['ts']
            if lo <= age <= hi:
                return True
    return False


def is_comex_opex_week() -> bool:
    """
    True if today is within 2 days of COMEX gold options expiry.
    COMEX Gold Options (symbol OG) expire on the 4th business day
    before the last business day of the month.

    During OPEX week, market makers push gold toward the 'max pain'
    level (price where most options expire worthless). This causes
    sudden $20-40 moves with NO news — the classic "why is gold
    moving?!" moment. Signals near OPEX should be treated with caution.
    """
    import calendar as cal_mod
    from datetime import date

    today = date.today()
    year, month = today.year, today.month

    # Last calendar day of the month
    last_day_num = cal_mod.monthrange(year, month)[1]
    last_date    = date(year, month, last_day_num)

    # Walk back to last BUSINESS day of the month
    while last_date.weekday() >= 5:   # 5=Sat, 6=Sun
        last_date -= timedelta(days=1)

    # Walk back 4 business days from last business day = COMEX expiry
    biz_back = 0
    expiry   = last_date
    while biz_back < 4:
        expiry -= timedelta(days=1)
        if expiry.weekday() < 5:
            biz_back += 1

    days_to_expiry = (expiry - today).days
    return -1 <= days_to_expiry <= 2   # day before, expiry day, day after


def get_gold_context() -> dict:
    """
    Returns a complete context snapshot for gold (XAUUSD) signals.
    Used by both the main signal engine and gold scalper to add a
    'second eye' — context that price action alone cannot see.

    Returns:
      fire_blocked     bool   — HIGH impact active, block FIRE locks
      caution_flag     bool   — MEDIUM impact active, WATCH only
      post_news_ob     bool   — 90-300s after HIGH = hunt OBs aggressively
      opex_week        bool   — COMEX gold options expiry within 2 days
      active_event     dict   — event currently in window (or None)
      upcoming_high    list   — HIGH impact events in next 4 hours
      upcoming_medium  list   — MEDIUM impact events in next 2 hours
      upcoming_low     list   — LOW impact events in next 1 hour
      context_label    str    — human-readable summary
    """
    _refresh_if_needed()
    now        = time.time()
    high_win   = _NEWS_WINDOW['high']
    med_win    = _NEWS_WINDOW['medium']
    low_win    = _NEWS_WINDOW['low']
    lo_ob, hi_ob = _POST_NEWS_OB_WINDOW

    fire_blocked  = False
    caution_flag  = False
    post_news_ob  = False
    active_event  = None
    up_high       = []
    up_medium     = []
    up_low        = []

    with _cache_lock:
        for ev in _cached_events:
            delta = ev['ts'] - now   # positive = future
            age   = -delta           # positive = past

            imp = ev['impact']

            # Active window check
            if imp == 'high' and abs(delta) <= high_win:
                fire_blocked = True
                active_event = dict(ev, seconds_until=int(delta))
            elif imp == 'medium' and abs(delta) <= med_win:
                caution_flag = True
                if active_event is None:
                    active_event = dict(ev, seconds_until=int(delta))

            # Post-news OB window
            if imp == 'high' and lo_ob <= age <= hi_ob:
                post_news_ob = True

            # Upcoming events (gold-relevant preferred)
            if delta > 0:
                if imp == 'high'   and delta <= 4 * 3600:
                    up_high.append(dict(ev, seconds_until=int(delta)))
                if imp == 'medium' and delta <= 2 * 3600:
                    up_medium.append(dict(ev, seconds_until=int(delta)))
                if imp == 'low'    and delta <= 3600:
                    up_low.append(dict(ev, seconds_until=int(delta)))

    opex = is_comex_opex_week()

    # Human-readable summary
    if fire_blocked:
        label = f"⛔ {active_event['title']} — FIRE blocked, wait for post-news OB"
    elif post_news_ob:
        label = "⚡ Post-news OB window — hunt institutional entry NOW"
    elif caution_flag and active_event:
        label = f"⚠️ {active_event['title']} — caution, WATCH only"
    elif opex:
        label = "🗓 COMEX OPEX week — unusual gold moves possible"
    elif up_high:
        mins = up_high[0]['seconds_until'] // 60
        label = f"📅 {up_high[0]['title']} in {mins}m"
    else:
        label = "✅ Clear — no major events"

    return {
        'fire_blocked':    fire_blocked,
        'caution_flag':    caution_flag,
        'post_news_ob':    post_news_ob,
        'opex_week':       opex,
        'active_event':    active_event,
        'upcoming_high':   up_high,
        'upcoming_medium': up_medium,
        'upcoming_low':    up_low,
        'context_label':   label,
    }


# ── AI Market Context Engine ────────────────────────────────────────────────────
#
# Rule-based knowledge base: what each economic event typically does to gold.
# This is not guesswork — these are well-established macro relationships
# that professional gold traders use every day.
#
# gold_dir values:
#   'bearish_if_strong'  — stronger print → USD up → Gold falls
#   'bullish_if_hot'     — hotter print → inflation fear → Gold up
#   'bearish_if_hawkish' — hawkish tone → rates up → Gold falls
#   'bearish_if_rise'    — rate rise → opportunity cost → Gold falls
#   'bullish_if_weak'    — weak data → risk-off / dovish Fed → Gold up
#   'complex'            — dual forces, depends on narrative
#   'volatile'           — just expect a big move either way
#   'neutral'            — minimal gold impact

_GOLD_EVENT_KNOWLEDGE = {
    'NFP':                  {'gold_dir': 'bearish_if_strong',  'magnitude': 'very_high',
                             'why': 'Strong jobs → USD rally → Gold sells off'},
    'Non-Farm':             {'gold_dir': 'bearish_if_strong',  'magnitude': 'very_high',
                             'why': 'Strong jobs → USD rally → Gold sells off'},
    'CPI':                  {'gold_dir': 'complex',            'magnitude': 'very_high',
                             'why': 'Hot CPI → hawkish Fed (USD up) but also inflation hedge demand — direction depends on magnitude'},
    'Inflation':            {'gold_dir': 'bullish_if_hot',     'magnitude': 'high',
                             'why': 'High inflation → gold demand as a store of value'},
    'PPI':                  {'gold_dir': 'bullish_if_hot',     'magnitude': 'high',
                             'why': 'Hot producer prices → inflation building → Gold demand'},
    'PCE':                  {'gold_dir': 'complex',            'magnitude': 'very_high',
                             'why': "Fed's preferred inflation gauge — hot print = hawkish = USD up but also inflation hedge"},
    'FOMC':                 {'gold_dir': 'bearish_if_hawkish', 'magnitude': 'very_high',
                             'why': 'Hawkish Fed minutes → higher rates → Gold falls sharply'},
    'Federal Open':         {'gold_dir': 'bearish_if_hawkish', 'magnitude': 'very_high',
                             'why': 'Fed policy decision — hawkish = USD strong = Gold weak'},
    'Interest Rate':        {'gold_dir': 'bearish_if_rise',    'magnitude': 'very_high',
                             'why': 'Rate hike → higher bond yields → Gold loses to safe yield'},
    'Federal Reserve':      {'gold_dir': 'bearish_if_hawkish', 'magnitude': 'very_high',
                             'why': 'Fed speakers → hawkish language tanks Gold'},
    'Powell':               {'gold_dir': 'bearish_if_hawkish', 'magnitude': 'high',
                             'why': 'Powell hawkish = rate hike expectation = USD up = Gold down'},
    'GDP':                  {'gold_dir': 'bearish_if_strong',  'magnitude': 'high',
                             'why': 'Strong growth → USD rally → mild Gold pressure'},
    'Unemployment':         {'gold_dir': 'bullish_if_high',    'magnitude': 'medium',
                             'why': 'High unemployment → risk-off sentiment → Gold demand'},
    'Jobless Claims':       {'gold_dir': 'bullish_if_high',    'magnitude': 'medium',
                             'why': 'More claims → weak labor → dovish Fed expectations → Gold up'},
    'Retail Sales':         {'gold_dir': 'bearish_if_strong',  'magnitude': 'medium',
                             'why': 'Strong consumer spending → USD up → Gold mild pressure'},
    'ISM':                  {'gold_dir': 'bearish_if_strong',  'magnitude': 'medium',
                             'why': 'Strong manufacturing → USD up → mild Gold pressure'},
    'PMI':                  {'gold_dir': 'bearish_if_strong',  'magnitude': 'medium',
                             'why': 'Expansion in PMI → USD up → Gold mild pressure'},
    'ADP':                  {'gold_dir': 'bearish_if_strong',  'magnitude': 'medium',
                             'why': 'Strong private jobs = NFP preview = USD up = Gold pressure'},
    'Treasury':             {'gold_dir': 'bearish_if_yield_up','magnitude': 'medium',
                             'why': 'Higher yields = opportunity cost for Gold = pressure'},
    'Bond':                 {'gold_dir': 'bearish_if_yield_up','magnitude': 'medium',
                             'why': 'Bond auctions affect yield expectations → Gold inverse'},
    'Durable Goods':        {'gold_dir': 'bearish_if_strong',  'magnitude': 'low',
                             'why': 'Strong orders → USD up → minimal Gold impact'},
    'Consumer Confidence':  {'gold_dir': 'bearish_if_high',    'magnitude': 'low',
                             'why': 'Confident consumers → risk-on → Gold mild pressure'},
    'Consumer Sentiment':   {'gold_dir': 'bearish_if_high',    'magnitude': 'low',
                             'why': 'High sentiment → risk appetite → Gold mild pressure'},
    'Housing':              {'gold_dir': 'neutral',            'magnitude': 'low',
                             'why': 'Housing data rarely moves Gold significantly'},
    'Trade Balance':        {'gold_dir': 'bearish_if_surplus', 'magnitude': 'low',
                             'why': 'Narrowing deficit → USD strength → mild Gold pressure'},
    'Industrial Production':{'gold_dir': 'bearish_if_strong',  'magnitude': 'low',
                             'why': 'Strong output → USD up → minimal Gold impact'},
    'Existing Home':        {'gold_dir': 'neutral',            'magnitude': 'low',
                             'why': 'Limited direct impact on Gold'},
    'Building Permits':     {'gold_dir': 'neutral',            'magnitude': 'low',
                             'why': 'Limited direct impact on Gold'},
}


def _match_event_knowledge(title: str) -> dict:
    """Match event title to the gold knowledge base. Returns the best match."""
    title_lower = title.lower()
    for keyword, knowledge in _GOLD_EVENT_KNOWLEDGE.items():
        if keyword.lower() in title_lower:
            return knowledge
    return {'gold_dir': 'volatile', 'magnitude': 'medium',
            'why': 'USD/macro event — expect elevated volatility on release'}


def _gold_dir_label(gold_dir: str, impact: str) -> tuple:
    """Returns (emoji, short_label, color_hint) for a given gold_dir."""
    labels = {
        'bearish_if_strong':  ('🐻', 'Bearish if strong',  'bear'),
        'bullish_if_hot':     ('🐂', 'Bullish if hot',     'bull'),
        'bearish_if_hawkish': ('🐻', 'Bearish if hawkish', 'bear'),
        'bearish_if_rise':    ('🐻', 'Bearish if rate up', 'bear'),
        'bullish_if_weak':    ('🐂', 'Bullish if weak',    'bull'),
        'bullish_if_high':    ('🐂', 'Bullish if high',    'bull'),
        'bearish_if_yield_up':('🐻', 'Bearish if yield up','bear'),
        'bearish_if_surplus': ('🐻', 'Bearish if surplus', 'bear'),
        'complex':            ('⚡', 'Complex — watch',    'neutral'),
        'volatile':           ('⚡', 'High volatility',    'neutral'),
        'neutral':            ('➖', 'Low gold impact',    'neutral'),
    }
    return labels.get(gold_dir, ('⚡', 'Watch release', 'neutral'))


def generate_market_context(events: list = None) -> dict:
    """
    Generate AI-style market intelligence for gold traders.

    Reads upcoming economic events and produces:
      - gold_stance:    'bullish' | 'bearish' | 'cautious' | 'volatile' | 'clear'
      - summary:        One-paragraph plain-English overview
      - trading_advice: Specific actionable advice right now
      - key_risks:      List of event names the trader must watch
      - event_impacts:  Per-event gold impact breakdown
      - macro_score:    -3 to +3 (negative = bearish gold pressure)

    This is rule-based intelligence, not guesswork.
    """
    if events is None:
        events = get_upcoming_events('all')

    now_ts = time.time()

    # Look at events in next 48h (to give full-day forward view)
    upcoming = [e for e in events
                if 0 <= e.get('seconds_until', -1) <= 172800]

    # Gold-relevant events only
    gold_events = [e for e in upcoming if e.get('gold_relevant') or e.get('impact') == 'high']

    if not gold_events:
        return {
            'gold_stance':    'clear',
            'summary':        'No major USD or gold-relevant events in the next 48 hours. Gold is free to trade on its own technical structure without macro interference. Trust your OB entries.',
            'trading_advice': 'Technical setups are fully valid. London and New York sessions will dominate with clean price action. Watch for institutional accumulation at key OB levels.',
            'key_risks':      [],
            'event_impacts':  [],
            'macro_score':    0,
            'next_event_ts':  None,
        }

    # Build per-event impact list
    event_impacts = []
    macro_score   = 0
    for ev in gold_events[:8]:   # cap at 8 events for readability
        knowledge = _match_event_knowledge(ev['title'])
        emoji, label, direction = _gold_dir_label(knowledge['gold_dir'], ev['impact'])

        hours_until = ev['seconds_until'] / 3600
        if hours_until < 0.33:
            timing_str = f"{int(ev['seconds_until'])}s"
        elif hours_until < 1:
            timing_str = f"{int(hours_until * 60)}min"
        elif hours_until < 24:
            timing_str = f"{hours_until:.1f}h"
        else:
            timing_str = f"{hours_until/24:.1f} days"

        # Forecast context
        fcast = ev.get('forecast', '')
        prev  = ev.get('previous', '')
        forecast_note = ''
        if fcast and prev:
            forecast_note = f'F:{fcast} vs P:{prev}'
        elif fcast:
            forecast_note = f'Forecast: {fcast}'

        # Macro score contribution
        gdir = knowledge['gold_dir']
        weight = {'very_high': 2, 'high': 1.5, 'medium': 1, 'low': 0.5}.get(knowledge['magnitude'], 1)
        if 'bearish' in gdir:
            macro_score -= weight
        elif 'bullish' in gdir:
            macro_score += weight

        event_impacts.append({
            'title':         ev['title'],
            'country':       ev['country'],
            'impact':        ev['impact'],
            'seconds_until': ev['seconds_until'],
            'timing':        timing_str,
            'gold_dir':      gdir,
            'direction':     direction,
            'emoji':         emoji,
            'label':         label,
            'why':           knowledge['why'],
            'forecast_note': forecast_note,
            'magnitude':     knowledge['magnitude'],
        })

    # Sort by time
    event_impacts.sort(key=lambda x: x['seconds_until'])

    # Overall stance
    macro_score = round(max(-3, min(3, macro_score / max(len(event_impacts), 1))), 2)

    if macro_score <= -1.0:
        stance = 'bearish'
        stance_emoji = '🐻'
    elif macro_score >= 1.0:
        stance = 'bullish'
        stance_emoji = '🐂'
    elif any(e['gold_dir'] == 'complex' and e['impact'] in ('high', 'very_high') for e in event_impacts):
        stance = 'volatile'
        stance_emoji = '⚡'
    elif len([e for e in event_impacts if e['impact'] == 'high']) >= 2:
        stance = 'cautious'
        stance_emoji = '⚠️'
    else:
        stance = 'cautious'
        stance_emoji = '👀'

    # Key risks (HIGH impact events only)
    key_risks = [e['title'] for e in event_impacts if e['impact'] in ('high',)]

    # Generate summary
    high_count = len([e for e in event_impacts if e['impact'] == 'high'])
    next_ev    = event_impacts[0] if event_impacts else None
    next_title = next_ev['title'] if next_ev else 'upcoming event'
    next_time  = next_ev['timing'] if next_ev else ''

    if stance == 'bearish':
        summary = (f"{stance_emoji} Macro backdrop is <b>bearish for gold</b>. "
                   f"{high_count} high-impact USD event{'s' if high_count!=1 else ''} ahead "
                   f"with historical tendency to strengthen the dollar. "
                   f"Favour SELL OBs and be cautious on BUY entries near event times. "
                   f"The post-news OB window after each release may offer the best entries.")
    elif stance == 'bullish':
        summary = (f"{stance_emoji} Macro backdrop is <b>supportive for gold</b>. "
                   f"Upcoming events trend toward USD weakness or risk-off sentiment. "
                   f"BUY OBs have macro tailwind. SELL entries carry extra risk. "
                   f"Watch for acceleration after releases.")
    elif stance == 'volatile':
        summary = (f"{stance_emoji} <b>High volatility expected</b>. "
                   f"Upcoming events (including {next_title}) have complex, unpredictable gold impact. "
                   f"Wait for the actual release before entering. The post-news OB pattern after these events "
                   f"is especially reliable — let the spike happen, then enter the retrace.")
    else:
        summary = (f"{stance_emoji} <b>Proceed with caution</b>. "
                   f"{high_count} significant event{'s' if high_count!=1 else ''} in the pipeline. "
                   f"Current OB setups are valid but may be interrupted. "
                   f"Keep positions sized conservatively near event times.")

    # Trading advice
    if next_ev:
        secs = next_ev['seconds_until']
        if secs < 120:
            trading_advice = (f"⛔ <b>{next_title} is LIVE now.</b> DO NOT open new trades. "
                              f"Wait 90 seconds for the spike to settle, then watch for the post-news OB.")
        elif secs < 1800:
            trading_advice = (f"⏰ <b>{next_title} in {next_time}.</b> "
                              f"Complete or avoid new entries. "
                              f"The system will automatically hunt the post-news OB after release.")
        elif secs < 7200:
            trading_advice = (f"📅 <b>{next_title} in {next_time}.</b> "
                              f"Current OB setups are valid now. "
                              f"Reduce size or avoid entries within 30min of the release.")
        else:
            trading_advice = (f"✅ Next key event: <b>{next_title}</b> in {next_time}. "
                              f"Current technical setups are fully valid. "
                              f"Trade freely on OB levels.")
    else:
        trading_advice = '✅ Calendar is clear. Trade all technical setups with full confidence.'

    return {
        'gold_stance':    stance,
        'stance_emoji':   stance_emoji,
        'summary':        summary,
        'trading_advice': trading_advice,
        'key_risks':      key_risks,
        'event_impacts':  event_impacts,
        'macro_score':    macro_score,
        'next_event_ts':  event_impacts[0]['seconds_until'] if event_impacts else None,
    }


def force_refresh():
    """Force an immediate calendar refresh (e.g. on app startup)."""
    global _cache_ts
    with _cache_lock:
        _cache_ts = 0.0
    _refresh_if_needed()
