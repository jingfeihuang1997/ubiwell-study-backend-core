"""
What the patient page shows, computed from what is already stored: watch series binned for
the chosen range, sleep estimated from the watch, which days have data, and check-in results
read back out of the Mind Guard log.

Times stay UTC instants here. The page sends the viewer's day as start/end instants and its
IANA time zone, so a day, and the night a sleep belongs to, follow the viewer.
"""

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from study_framework_core.core.config import get_config

# (collection, value field, key, label, unit, how a bucket is reduced)
SERIES = [
    ('GARMIN_HR', 'heart_rate', 'heart_rate', 'Heart rate', 'bpm', 'mean'),
    ('GARMIN_STRESS', 'heart_rate', 'stress', 'Stress', 'score', 'mean'),       # stress score is stored under heart_rate
    ('GARMIN_STRESS', 'body_battery', 'body_battery', 'Body Battery', 'score', 'mean'),
    ('GARMIN_RESPIRATION', 'respiration', 'respiration', 'Breathing rate', 'brpm', 'mean'),
    ('GARMIN_STEPS', 'steps', 'steps', 'Steps', 'steps', 'sum'),
]

# Stress and respiration statuses that mean the watch was on the wrist.
WORN = {'VALID', 'LARGE_MOTION', 'UNIDENTIFIED', 'NOT_ENOUGH_DATA'}

# Sleep estimate: still minutes on the wrist, joined across short wake-ups.
SLEEP_GAP_MIN = 20          # a break longer than this ends a block
MAIN_SLEEP_MIN = 180        # a block this long, mostly at night, is the night's sleep
NAP_MIN, NAP_MAX = 20, 120  # a shorter daytime block with a lower heart rate is a nap
NIGHT = (21, 10)            # local hours that count as night: 21:00 to 10:00
NAP_HR_DROP = 5             # bpm below the day's median heart rate for a minute to count as asleep
HR_WINDOW_MIN = 5           # minutes either side whose heart rate decides if a still minute is asleep
ASLEEP_HR_ABOVE_LOW = 12    # bpm above the range's lowest tenth of heart rates that still counts as asleep


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def epoch_seconds(ts):
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts):
        return None
    return ts / 1000 if ts > 1e11 else ts       # some sources write milliseconds


def _range_query(uid, lo, hi):
    # Timestamps may be seconds or milliseconds; ask for both.
    return {'uid': uid, '$or': [{'timestamp': {'$gte': lo, '$lt': hi}},
                                {'timestamp': {'$gte': lo * 1000, '$lt': hi * 1000}}]}


def _iso(sec):
    return datetime.fromtimestamp(sec, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def bucket_seconds(lo, hi):
    """One point a minute for a day, every 10 minutes for longer ranges."""
    return 60 if hi - lo <= 2 * 86400 else 600


# Fields read from each collection; one read per collection serves the series and the sleep estimate.
WATCH_FIELDS = {'GARMIN_HR': ('heart_rate', 'status'), 'GARMIN_STRESS': ('heart_rate', 'body_battery', 'status'),
                'GARMIN_RESPIRATION': ('respiration', 'status'), 'GARMIN_STEPS': ('steps',)}
SLEEP_LOOKBACK = 12 * 3600      # a night that ends in the range starts before it


def load_watch(db, uid, lo, hi):
    """Every watch reading for [lo, hi), one query per collection, timestamps as epoch seconds."""
    cols = get_config().collections
    out = {}
    for coll, fields in WATCH_FIELDS.items():
        proj = {'_id': 0, 'timestamp': 1, **{f: 1 for f in fields}}
        rows = []
        for d in db[getattr(cols, coll)].find(_range_query(uid, lo, hi), proj):
            t = epoch_seconds(d.get('timestamp'))
            if t is not None:
                d['t'] = t
                rows.append(d)
        out[coll] = rows
    return out


def watch_series(db, uid, lo, hi, watch=None):
    """Each series binned for [lo, hi): points are [bucket start, value], NaN readings skipped."""
    watch = watch if watch is not None else load_watch(db, uid, lo, hi)
    step = bucket_seconds(lo, hi)
    out = []
    for coll, field, key, label, unit, how in SERIES:
        buckets = {}
        for d in watch[coll]:
            t, v = d['t'], d.get(field)
            # The watch writes -1 or -2 for "no reading" (off the wrist, too much motion); a heart rate
            # or breathing rate of 0 is no reading either.
            if t < lo or t >= hi or not finite(v) or v < 0 or (v == 0 and key in ('heart_rate', 'respiration')):
                continue
            b = int(t // step * step)
            s = buckets.setdefault(b, [0.0, 0])
            s[0] += v
            s[1] += 1
        points = [[b, round(s[0] if how == 'sum' else s[0] / s[1], 2)] for b, s in sorted(buckets.items())]
        out.append({'key': key, 'label': label, 'unit': unit, 'agg': how, 'bucket': step, 'points': points})
    return out


# ---------------------------------------------------------------------------
# Extra measures: sent by the watch or phone but not on the page by default. The page lists the
# ones a participant has under "+" on the metric row and asks for them by key.
# ---------------------------------------------------------------------------
EXTRAS = {          # key: (label, unit, how a day is reduced, where it comes from)
    'hrv': ('Heart rate variability', 'ms', 'mean', 'RMSSD of the beat-to-beat intervals the watch records'),
    'worn': ('Watch worn', 'min', 'sum', 'minutes the watch reported being on the wrist'),
    'phone_battery': ('Phone battery', '%', 'mean', 'battery level the phone app reports'),
}
IBI_OK = (300, 2000)        # ms; beat intervals outside this are artifacts
IBI_MAX_JUMP = 0.2          # a change of more than 20% from one beat to the next is an artifact
IBI_MIN_PAIRS = 30          # fewer successive pairs in a bucket than this give no HRV value


def available_extras(db, uid):
    """Which extra measures this participant has any data for."""
    cols = get_config().collections
    have = []
    if db[cols.GARMIN_IBI].find_one({'uid': uid}, {'_id': 1}):
        have.append('hrv')
    if db[cols.GARMIN_STRESS].find_one({'uid': uid, 'status': {'$in': sorted(WORN)}}, {'_id': 1}):
        have.append('worn')
    if db[cols.IOS_BATTERY].find_one({'uid': uid}, {'_id': 1}):
        have.append('phone_battery')
    return have


def _series(key, points, step):
    label, unit, how, _ = EXTRAS[key]
    return {'key': key, 'label': label, 'unit': unit, 'agg': how, 'bucket': step, 'points': points, 'extra': True}


def extra_series(db, uid, lo, hi, keys, watch=None):
    """The extra measures asked for, binned like watch_series. HRV buckets are at least 5 minutes,
    since one minute holds too few beats for a steady value."""
    cols = get_config().collections
    step = bucket_seconds(lo, hi)
    out = []
    for key in [k for k in EXTRAS if k in keys]:
        if key == 'hrv':
            hstep = max(step, 300)
            beats = []
            for d in db[cols.GARMIN_IBI].find(_range_query(uid, lo, hi), {'_id': 0, 'timestamp': 1, 'bbi': 1}):
                t, v = epoch_seconds(d.get('timestamp')), d.get('bbi')
                if t is not None and finite(v) and IBI_OK[0] <= v <= IBI_OK[1]:
                    beats.append((t, float(v)))
            beats.sort()
            sq = {}
            for (t0, a), (t1, b) in zip(beats, beats[1:]):
                if t1 - t0 > 3 or abs(b - a) > IBI_MAX_JUMP * a:
                    continue
                s_ = sq.setdefault(int(t1 // hstep * hstep), [0.0, 0])
                s_[0] += (b - a) ** 2
                s_[1] += 1
            pts = [[k, round(math.sqrt(v[0] / v[1]), 1)] for k, v in sorted(sq.items()) if v[1] >= IBI_MIN_PAIRS]
            out.append(_series(key, pts, hstep))
        elif key == 'worn':
            watch = watch if watch is not None else load_watch(db, uid, lo, hi)
            minutes = set()
            for coll in ('GARMIN_STRESS', 'GARMIN_RESPIRATION'):
                for d in watch[coll]:
                    if lo <= d['t'] < hi and str(d.get('status') or '') in WORN:
                        minutes.add(int(d['t'] // 60))
            per = {}
            for m in minutes:
                b = int(m * 60 // step * step)
                per[b] = per.get(b, 0) + 1
            out.append(_series(key, [[b, n] for b, n in sorted(per.items())], step))
        elif key == 'phone_battery':
            per = {}
            for d in db[cols.IOS_BATTERY].find(_range_query(uid, lo, hi), {'_id': 0, 'timestamp': 1, 'battery_left': 1}):
                t, v = epoch_seconds(d.get('timestamp')), d.get('battery_left')
                if t is None or not finite(v) or not (lo <= t < hi):
                    continue
                v = v * 100 if 0 < v <= 1 else v          # some phones report a fraction
                s_ = per.setdefault(int(t // step * step), [0.0, 0])
                s_[0] += v
                s_[1] += 1
            out.append(_series(key, [[b, round(v[0] / v[1], 1)] for b, v in sorted(per.items())], step))
    return out


# ---------------------------------------------------------------------------
# Questionnaires (GAD-7, PHQ-9, PCSS). Nothing sends them yet: the page shows "not collected" until
# a source writes to this collection, one document per completed questionnaire:
#   {uid, scale: 'GAD-7' | 'PHQ-9' | 'PCSS', completed_at: ISO UTC, total?, source?,
#    items: [{question, answer, score}]}
# ---------------------------------------------------------------------------
QUESTIONNAIRES = 'questionnaires'
SCALES = [      # code, name, highest score, (upper bound, band) from the scale's published cut-offs, what it measures
    ('GAD-7', 'Generalized Anxiety Disorder 7-item Scale', 21,
     [(4, 'Minimal'), (9, 'Mild'), (14, 'Moderate'), (21, 'Severe')], 'anxiety symptoms'),
    ('PHQ-9', 'Patient Health Questionnaire-9', 27,
     [(4, 'Minimal'), (9, 'Mild'), (14, 'Moderate'), (19, 'Moderately severe'), (27, 'Severe')], 'depression symptoms'),
    ('PCSS', 'Post-Concussion Symptom Scale', 132, None, 'concussion symptoms'),
]


def _band(total, bands):
    if total is None or not bands:
        return None
    for top, name in bands:
        if total <= top:
            return name
    return bands[-1][1]


def _questionnaire(doc, bands):
    if not doc:
        return None
    items = [{'question': i.get('question'), 'answer': i.get('answer'), 'score': i.get('score') if finite(i.get('score')) else None}
             for i in doc.get('items') or [] if isinstance(i, dict)]
    total = doc.get('total') if finite(doc.get('total')) else (sum(i['score'] for i in items if i['score'] is not None) if items else None)
    return {'at': doc.get('completed_at'), 'total': total, 'band': _band(total, bands), 'source': doc.get('source'), 'items': items}


def questionnaire_results(db, uid, end_iso):
    """For each scale, the latest questionnaire completed before end_iso and the one before it."""
    rows = []
    for code, name, top, bands, what in SCALES:
        docs = list(db[QUESTIONNAIRES].find({'uid': uid, 'scale': code, 'completed_at': {'$lt': end_iso}},
                                            {'_id': 0}).sort('completed_at', -1).limit(2))
        rows.append({'scale': code, 'name': name, 'max': top, 'what': what, 'has_bands': bool(bands),
                     'latest': _questionnaire(docs[0] if docs else None, bands),
                     'previous': _questionnaire(docs[1] if len(docs) > 1 else None, bands)})
    return rows


def _minute_signals(watch):
    """Per minute: was the watch worn, was there motion, heart rates seen."""
    worn, motion, hr = set(), set(), {}
    for coll in ('GARMIN_STRESS', 'GARMIN_RESPIRATION'):
        for d in watch[coll]:
            m = int(d['t'] // 60)
            status = str(d.get('status') or '')
            if status in WORN:
                worn.add(m)
            if status == 'LARGE_MOTION':
                motion.add(m)
    for d in watch['GARMIN_STEPS']:
        v = d.get('steps')
        if finite(v) and v > 0:
            motion.add(int(d['t'] // 60))
    for d in watch['GARMIN_HR']:
        v = d.get('heart_rate')
        if not finite(v) or v <= 0:
            continue
        m = int(d['t'] // 60)
        worn.add(m)
        hr.setdefault(m, []).append(v)
    return worn, motion, hr


def _night_minutes(start_min, end_min, tz):
    n = 0
    for m in range(start_min, end_min + 1):
        h = datetime.fromtimestamp(m * 60, tz).hour
        if h >= NIGHT[0] or h < NIGHT[1]:
            n += 1
    return n


def estimate_sleep(db, uid, lo, hi, tz_name, watch=None):
    """Sleep and naps estimated from the watch in [lo, hi). Each has start/end instants, still
    minutes, minutes awake inside it, and the average heart rate. An estimate, not a sleep stage
    measurement: the watch does not report sleep itself."""
    tz = ZoneInfo(tz_name)
    watch = watch if watch is not None else load_watch(db, uid, lo - SLEEP_LOOKBACK, hi)
    worn, motion, hr = _minute_signals(watch)

    def is_night(m):
        h = datetime.fromtimestamp(m * 60, tz).hour
        return h >= NIGHT[0] or h < NIGHT[1]
    day_hr = [v for m, vs in hr.items() if not is_night(m) for v in vs]
    day_median = statistics.median(day_hr) if day_hr else None
    # The day's median includes walking about, so on its own it sits too high: an evening sitting
    # still would pass. The lowest tenth of the range's heart rates is close to the sleeping rate
    # when the watch was worn at night, so "asleep" is capped a little above that as well.
    all_hr = sorted(v for vs in hr.values() for v in vs)
    low = all_hr[int(0.1 * (len(all_hr) - 1))] if all_hr else None
    caps = [c for c in (day_median - NAP_HR_DROP if day_median is not None else None,
                        low + ASLEEP_HR_ABOVE_LOW if low is not None else None) if c is not None]
    asleep_hr = min(caps) if caps else None

    def calm(m):
        # Sitting still awake (reading, a screen) has the day's heart rate; asleep it is lower.
        # Judged on the median of the minutes around, so one high or low reading does not decide.
        if asleep_hr is None:
            return True
        near = [v for k in range(m - HR_WINDOW_MIN, m + HR_WINDOW_MIN + 1) for v in hr.get(k, [])]
        return not near or statistics.median(near) <= asleep_hr

    still = sorted(m for m in worn if m not in motion and calm(m))
    blocks, cur = [], None
    for m in still:
        if cur and m - cur['last'] <= SLEEP_GAP_MIN:
            cur['last'] = m
            cur['still'] += 1
        else:
            if cur:
                blocks.append(cur)
            cur = {'first': m, 'last': m, 'still': 1}
    if cur:
        blocks.append(cur)

    still_set = set(still)
    out = []
    for b in blocks:
        span = b['last'] - b['first'] + 1
        if b['last'] * 60 < lo or b['first'] * 60 >= hi:
            continue
        # Awake only where the watch showed it (moving, or a waking heart rate). A minute with no
        # reading is a gap in the upload, not a wake-up, so it stays part of the sleep.
        awake = sum(1 for m in range(b['first'], b['last'] + 1) if m in worn and m not in still_set)
        asleep = span - awake
        night = _night_minutes(b['first'], b['last'], tz)
        rates = [v for m in range(b['first'], b['last'] + 1) for v in hr.get(m, [])]
        avg = round(sum(rates) / len(rates), 1) if rates else None
        kind = None
        if asleep >= MAIN_SLEEP_MIN and night >= span / 2:
            kind = 'sleep'
        elif NAP_MIN <= asleep <= NAP_MAX and night < span / 2 and avg is not None \
                and day_median is not None and avg <= day_median - NAP_HR_DROP:
            kind = 'nap'
        if kind:
            out.append({'kind': kind, 'start': _iso(b['first'] * 60), 'end': _iso((b['last'] + 1) * 60),
                        'minutes': asleep, 'awake_minutes': awake, 'avg_hr': avg})
    return out


def calendar_presence(db, uid, lo, hi):
    """Which instants in [lo, hi) have a check-in or a valid watch reading, for the calendar.
    Watch readings are reduced to the hours that have one; the page buckets both into its own days."""
    cols = get_config().collections
    hours = set()
    for d in db[cols.GARMIN_HR].find(_range_query(uid, lo, hi), {'_id': 0, 'timestamp': 1, 'heart_rate': 1}):
        t = epoch_seconds(d.get('timestamp'))
        if t is not None and finite(d.get('heart_rate')) and d['heart_rate'] > 0:
            hours.add(int(t // 3600) * 3600)
    for d in db[cols.GARMIN_STRESS].find(dict(_range_query(uid, lo, hi), status='VALID'), {'_id': 0, 'timestamp': 1}):
        t = epoch_seconds(d.get('timestamp'))
        if t is not None:
            hours.add(int(t // 3600) * 3600)
    checkins = [c['started_at'] for c in db['mindguard_checkins'].find(
        {'uid': uid, 'started_at': {'$gte': _iso(lo), '$lt': _iso(hi)}}, {'_id': 0, 'started_at': 1})
        if c.get('started_at')]
    return {'checkins': sorted(checkins), 'watch_hours': sorted(hours)}


def last_activity(db, uid):
    """The latest check-in start (ISO) and the latest valid watch reading (epoch seconds), ever."""
    cols = get_config().collections
    c = db['mindguard_checkins'].find_one({'uid': uid, 'started_at': {'$type': 'string'}},
                                          {'_id': 0, 'started_at': 1}, sort=[('started_at', -1)])
    last_watch = None
    for d in db[cols.GARMIN_HR].find({'uid': uid, 'heart_rate': {'$gt': 0}}, {'_id': 0, 'timestamp': 1}).sort('timestamp', -1).limit(1):
        last_watch = epoch_seconds(d.get('timestamp'))
    return {'last_checkin': (c or {}).get('started_at'), 'last_watch': last_watch}


# ---------------------------------------------------------------------------
# Check-in results read from the Mind Guard log
# ---------------------------------------------------------------------------
NUM_RX = re.compile(r'\b(10|[0-9])\b')
WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}


def _num(a):
    if finite(a.get('num')):
        return a['num']
    text = str(a.get('answer') or '').lower()
    m = NUM_RX.search(text)
    if m:
        return int(m.group(1))
    for w, n in WORDS.items():
        if re.search(r'\b' + w + r'\b', text):
            return n
    return None


def _choice(a, options):
    if a.get('key') in options:
        return a['key']
    text = str(a.get('answer') or '').lower()
    for k, words in options.items():
        if any(w in text for w in words):
            return k
    return None


MOOD = {'low': ('low', 'bad', 'sad', 'down'), 'mixed': ('mixed', 'up and down', 'so so', 'okay', 'ok'), 'good': ('good', 'great', 'fine')}
TREND = {'worse': ('worse',), 'better': ('better', 'improv'), 'same': ('same', 'no change')}
PLAN = {'none': ('none', 'not at all', "didn't", 'did not'), 'some': ('some', 'part', 'half', 'a little'), 'all': ('all', 'every', 'most')}


def _answers(c, nodes=None, item=None):
    return [a for a in c.get('answers') or []
            if (nodes is None or a.get('node') in nodes) and (item is None or a.get('item') == item)]


def _qa(answers):
    """The questions as Mind Guard asked them and the words Alexa heard, in the order asked."""
    return [{'at': a.get('at'), 'question': a.get('question'), 'answer': a.get('answer')} for a in answers]


def _entry(c, value, text, severity, answers=()):
    return {'at': c.get('started_at'), 'checkin_id': c.get('checkin_id'), 'value': value, 'text': text,
            'severity': severity, 'qa': _qa(answers)}


# Which questions of the skill (lambda/index.js node ids) each result is read from.
SYMPTOM_NODES = ('sx_main', 'sx_which', 'sx_h', 'sx_d', 'sx_n', 'sx_v', 'sx_l')
PLAN_NODES = ('p_ask', 'p_confirm', 'p_free', 'p_main', 'p_barrier', 'p_stopwhy')
EXERTION_NODES = ('p_sym', 'p_before', 'p_after', 'p_settle', 'p_stopped')
MOOD_NODES = ('m_gate', 'm_why', 'm_hopeless')
# Categories the clinicians asked for, asked by Mind Guard since Nov 2026: fatigue and anxiety every day
# (with a 0 to 10 rating), irritability and appetite every 2nd day and on mixed or low mood days.
ANXIETY_NODES = ('an_main', 'an_level')
IRRITABILITY_NODES = ('ir_main',)
FATIGUE_NODES = ('fa_main', 'fa_level')
APPETITE_NODES = ('ap_main',)
APPETITE = {'less': ('less', 'not hungry', 'lower', 'no appetite', 'barely', 'skipp'), 'more': ('more', 'hungrier', 'bigger', 'a lot'),
            'normal': ('normal', 'usual', 'same', 'fine', 'good', 'ok')}

# (key, label, the skill's question wording, how the result is read, asked by Mind Guard today)
MEASURES = [
    ('warning_signs', 'Warning signs',
     'Headache getting worse and worse, throwing up more than once, confusion, slurred speech, weakness on one side, '
     'seizure, passing out, or could not be woken (three questions, with follow-ups)',
     'Number of signs not answered with a clear no. Red when the skill raised an emergency.', True),
    ('asq', 'Suicidality (ASQ screen)', 'The four NIMH ASQ questions, asked when mood answers call for it',
     'Positive when any answer is not a clear no.', True),
    ('mood', 'Depression / low mood', 'How has your mood been overall: mostly good, mixed, or mostly low? (then: have you felt hopeless?)',
     'The choice picked; severe when they also said they felt hopeless.', True),
    ('anxiety', 'Anxiety', 'In the past day, have you felt nervous, worried, or on edge? How strong, from 0 to 10?',
     'The rating, or reported / none.', True),
    ('headache', 'Headache (worst)', 'For the headache: at its worst, how strong was it, from 0 to 10?',
     'The number given. Asked only when a headache was named.', True),
    ('symptoms', 'Symptoms', 'Have you had any headaches, dizziness, nausea, trouble seeing, or problems with light or noise?',
     'Reported or none, with the symptoms named.', True),
    ('symptom_trend', 'Symptom trend', 'Compared with last time, are your symptoms better, about the same, or worse?',
     'The choice picked.', True),
    ('fatigue', 'Fatigue', 'Have you felt more tired or low on energy than usual? How strong, from 0 to 10?',
     'The rating, or reported / none.', True),
    ('irritability', 'Irritability', 'Have you felt more irritable or easily annoyed than usual?', 'Reported or none.', True),
    ('appetite', 'Appetite / nutrition', 'How has your appetite been: about normal, less than usual, or more than usual?',
     'The choice picked; less than usual is flagged.', True),
    ('plan', 'Recovery plan', 'How much of your plan have you been able to do: all of it, some of it, or not really?',
     'The choice picked.', True),
    ('exertion', 'Symptoms with activity', 'Before you started, how bad were your symptoms, from 0 to 10? And at the worst point?',
     'Change from before to the worst point. Asked only when symptoms came on with activity.', True),
]


def check_in_results(checkins):
    """One row per measure, always all of them; `entries` newest first, empty when it was not asked.
    Each entry keeps the questions and answers it was read from. severity: high, mid, low, none."""
    found = {m[0]: [] for m in MEASURES}

    def yes_level(c, key, main_node, level_node=None):
        # "Have you felt ...?" (yes/no), then, when there is one, "how strong, 0 to 10?"
        main, lvl = _answers(c, nodes=(main_node,)), _answers(c, nodes=(level_node,)) if level_node else []
        if not main and not lvl:
            return
        n = _num(lvl[-1]) if lvl else None
        yes = bool(main) and main[-1].get('cls') in ('yes', 'unsure', 'answer')
        if n is not None:
            found[key].append(_entry(c, n, f'{n} / 10', 'high' if n >= 7 else ('mid' if n >= 4 else ('low' if n else 'none')),
                                     main + lvl))
        else:
            found[key].append(_entry(c, 1 if yes else 0, 'Reported' if yes else 'None reported', 'mid' if yes else 'none', main))

    for c in checkins:
        yes_level(c, 'anxiety', 'an_main', 'an_level')
        yes_level(c, 'fatigue', 'fa_main', 'fa_level')
        yes_level(c, 'irritability', 'ir_main')
        for a in _answers(c, nodes=APPETITE_NODES):
            k = _choice(a, APPETITE)
            if k:
                found['appetite'].append(_entry(c, {'normal': 0, 'more': 1, 'less': 2}[k],
                                                {'normal': 'About normal', 'more': 'More than usual', 'less': 'Less than usual'}[k],
                                                {'normal': 'none', 'more': 'low', 'less': 'mid'}[k], [a]))
        danger = _answers(c, item='danger')
        if danger:
            hits = [a for a in danger if a.get('cls') in ('yes', 'unsure', 'concern')]
            emerg = [f for f in c.get('flags') or [] if f.get('level') == 'EMERG' and f.get('item') == 'danger']
            n = len(hits)
            found['warning_signs'].append(_entry(c, n, (f'{n} sign{"s" if n != 1 else ""} reported' if n else 'None reported'),
                                                 'high' if emerg else ('mid' if n else 'none'), danger))

        for a in _answers(c, nodes=('h_worst',)):
            n = _num(a)
            if n is not None:
                found['headache'].append(_entry(c, n, f'{n} / 10', 'high' if n >= 7 else ('mid' if n >= 4 else 'low'),
                                                _answers(c, nodes=('h_worst', 'h_worse'))))

        main = _answers(c, nodes=('sx_main',))
        if main:
            named = [s for s in c.get('summary') or [] if not s.startswith('[private]')]
            yes = main[-1].get('cls') in ('yes', 'unsure', 'answer')
            e = _entry(c, 1 if yes else 0, ('Reported' if yes else 'None reported'), 'mid' if yes else 'none',
                       _answers(c, nodes=SYMPTOM_NODES))
            if yes and named:
                e['detail'] = '; '.join(named[:4])
            found['symptoms'].append(e)

        for a in _answers(c, nodes=('sx_trend',)):
            k = _choice(a, TREND)
            if k:
                found['symptom_trend'].append(_entry(c, {'better': 0, 'same': 1, 'worse': 2}[k],
                                                     {'better': 'Better', 'same': 'About the same', 'worse': 'Worse'}[k],
                                                     'mid' if k == 'worse' else 'none', [a]))

        for a in _answers(c, nodes=('m_gate',)):
            k = _choice(a, MOOD)
            if k:
                hopeless = [h for h in _answers(c, nodes=('m_hopeless',)) if h.get('cls') != 'no']
                if hopeless:
                    found['mood'].append(_entry(c, 3, ('Mostly low' if k == 'low' else 'Mixed') + ', felt hopeless', 'high',
                                                _answers(c, nodes=MOOD_NODES)))
                else:
                    found['mood'].append(_entry(c, {'good': 0, 'mixed': 1, 'low': 2}[k],
                                                {'good': 'Mostly good', 'mixed': 'Mixed', 'low': 'Mostly low'}[k],
                                                {'good': 'none', 'mixed': 'low', 'low': 'mid'}[k], _answers(c, nodes=MOOD_NODES)))

        for a in _answers(c, nodes=('p_main',)):
            k = _choice(a, PLAN)
            if k:
                found['plan'].append(_entry(c, {'all': 0, 'some': 1, 'none': 2}[k],
                                            {'all': 'All of it', 'some': 'Some of it', 'none': 'None of it'}[k],
                                            {'all': 'none', 'some': 'low', 'none': 'mid'}[k], _answers(c, nodes=PLAN_NODES)))

        before, after = _answers(c, nodes=('p_before',)), _answers(c, nodes=('p_after',))
        if before and after:
            b, a = _num(before[-1]), _num(after[-1])
            if b is not None and a is not None:
                rise = a - b
                found['exertion'].append(_entry(c, rise, f'{b} → {a} (+{rise})' if rise >= 0 else f'{b} → {a} ({rise})',
                                                'mid' if rise > 2 else 'none', _answers(c, nodes=EXERTION_NODES)))

        r = c.get('asq') or {}
        if any(k in r for k in ('q1', 'q2', 'q3', 'q4')):
            positive = any(r.get(k) not in (None, 'no') for k in ('q1', 'q2', 'q3', 'q4'))
            acute = any(f.get('level') == 'EMERG' and f.get('item') == 'safety' for f in c.get('flags') or [])
            e = _entry(c, 1 if positive else 0, ('Positive, acute' if acute else 'Positive') if positive else 'Negative',
                       'high' if positive else 'none', _answers(c, item='safety'))
            e['detail'] = ' · '.join(f'{k.upper()} {r[k]}' for k in ('q1', 'q2', 'q3', 'q4') if k in r)
            found['asq'].append(e)

    rows = []
    for key, label, question, reading, in_skill in MEASURES:
        entries = sorted(found[key], key=lambda e: e['at'] or '', reverse=True)
        trend = None
        nums = [e['value'] for e in entries if finite(e['value'])]
        if len(nums) >= 2 and nums[0] != nums[1]:
            trend = 'worse' if nums[0] > nums[1] else 'better'     # every value is ordered so higher is worse
        rows.append({'key': key, 'label': label, 'question': question, 'reading': reading, 'in_skill': in_skill, 'entries': entries,
                     'latest': entries[0] if entries else None, 'trend': trend})
    return rows


def status_dot(db, uid, now=None):
    """red: an EMERG or SAME-DAY flag in the last 7 days; green: data in the last 3 days; grey: neither."""
    now = now or datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    for c in db['mindguard_checkins'].find({'uid': uid, 'started_at': {'$gte': week}}, {'_id': 0, 'flags': 1}):
        if any(f.get('level') in ('EMERG', 'SAME-DAY') for f in c.get('flags') or []):
            return 'red'
    last = last_activity(db, uid)
    recent = (now - timedelta(days=3))
    if (last['last_checkin'] and last['last_checkin'] >= recent.strftime('%Y-%m-%dT%H:%M:%S.000Z')) or \
            (last['last_watch'] and last['last_watch'] >= recent.timestamp()):
        return 'green'
    return 'grey'
