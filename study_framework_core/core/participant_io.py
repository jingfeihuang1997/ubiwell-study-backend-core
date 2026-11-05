"""
Export one participant's data as a ZIP, and import a participant file back.

The ZIP (format "mindguard-participant", version 1):
  participant.json        everything the site entered or Mind Guard collected: profile and clinical
                          info, check-ins (every question and answer), questionnaires, sleep estimates.
                          This is the file the import reads.
  checkins.csv            one row per answer, for analysis
  results.csv             the check-in results the patient page shows, one row per measure and check-in
  sleep_estimates.csv     estimated nights and naps
  watch/*.csv, phone/*.csv  raw readings in the range, as stored (no binning)
  README.txt              what each file and column means

Never exported: the app password (study_pass), the Garmin password, the file encryption key, login
codes' access history, web login hashes, push tokens.

Import reads participant.json (from the ZIP or on its own) and, after showing what would change,
adds profile fields, check-ins (the newer copy of a check-in wins, as with the live intake) and
questionnaires to the participant chosen. Watch data is not imported: it only comes from the phone app.
"""

import csv
import io
import json
import math
import re
import zipfile
from datetime import datetime, timezone

from study_framework_core.core import mindguard, participants, patient_view
from study_framework_core.core.config import get_config

FORMAT, VERSION = 'mindguard-participant', 1
MAX_IMPORT_BYTES = 20 * 1024 * 1024
PROFILE_KEYS = ('name', 'age', 'gender', 'injury_date', 'psych_history', 'psych_note', 'mh_prescriptions',
                'mh_prescription_names', 'sleep_target_hours', 'steps_target', 'activity_note')

# (file, collection, value columns: (csv header, stored field)), all with time_utc and epoch_s first
RAW = [
    ('watch/heart_rate.csv', 'GARMIN_HR', [('heart_rate_bpm', 'heart_rate'), ('status', 'status')]),
    ('watch/stress_body_battery.csv', 'GARMIN_STRESS', [('stress_score', 'heart_rate'), ('body_battery', 'body_battery'),
                                                        ('status', 'status')]),
    ('watch/respiration.csv', 'GARMIN_RESPIRATION', [('breaths_per_min', 'respiration'), ('status', 'status')]),
    ('watch/steps.csv', 'GARMIN_STEPS', [('steps', 'steps'), ('total_steps', 'total_steps')]),
    ('phone/battery.csv', 'IOS_BATTERY', [('battery_percent', 'battery_left')]),
]
BEATS = ('watch/beat_intervals.csv', 'GARMIN_IBI', [('beat_interval_ms', 'bbi')])

README = """Mind Guard participant export
==============================
Participant: {uid}    Exported: {exported} by {by}    Range: {range}
Format: mindguard-participant, version 1. Times are UTC (ISO 8601) unless a column says otherwise;
epoch_s is seconds since 1970-01-01 UTC.

participant.json      Profile and clinical info entered on the site, every Mind Guard check-in with
                      each question as asked and the words Alexa heard, questionnaires, sleep estimates.
                      The site's Import button reads this file.
checkins.csv          One row per answer: checkin_id, check-in date (the skill's local date), status,
                      started_at, answered_at, item and node (which question), question, answer,
                      cls (how the skill read it: yes, no, unsure, answer, decline, concern),
                      choice (the option picked, e.g. mood "low"), rating (a 0 to 10 number, when asked).
results.csv           What the patient page shows under Check-in results, per check-in and measure:
                      value (ordered so higher is worse), text, severity (none, low, mid, high).
sleep_estimates.csv   Nights and naps estimated from the watch (no sleep stages; an estimate from wear,
                      stillness and heart rate). minutes = asleep, awake_minutes = awake inside it.
watch/heart_rate.csv           heart_rate_bpm; 0 or negative means the watch had no reading.
watch/stress_body_battery.csv  stress_score 0 to 100 (negative: no reading), body_battery 0 to 100,
                               status as the watch reports it (VALID, OFF_WRIST, LARGE_MOTION, ...).
watch/respiration.csv          breaths_per_min (negative: no reading) and status.
watch/steps.csv                steps in the interval, total_steps for the day when the watch sends it.
watch/beat_intervals.csv       beat-to-beat interval in ms (only when chosen; large).
phone/battery.csv              phone battery percent reported by the UbiWell app.

Not included: app and Garmin passwords, encryption keys, web login details.
"""


def _iso(sec):
    return datetime.fromtimestamp(sec, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _json_safe(v):
    """NaN and infinities become null, datetimes ISO strings, ObjectIds strings."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items() if k != '_id'}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if type(v).__name__ == 'ObjectId':
        return str(v)
    return v


def _csv(rows, header):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue()


def _raw_rows(db, uid, coll, cols, lo, hi):
    q = {'uid': uid}
    if lo is not None:
        q = patient_view._range_query(uid, lo, hi)
    fields = {'_id': 0, 'timestamp': 1, **{f: 1 for _, f in cols}}
    rows = []
    for d in db[getattr(get_config().collections, coll)].find(q, fields):
        t = patient_view.epoch_seconds(d.get('timestamp'))
        if t is None:
            continue
        vals = []
        for _, f in cols:
            v = d.get(f)
            vals.append('' if v is None or (isinstance(v, float) and not math.isfinite(v)) else v)
        rows.append([_iso(t), round(t, 3)] + vals)
    rows.sort(key=lambda r: r[1])
    return rows


def export_bundle(db, uid, lo=None, hi=None, beats=False, by='admin', tz='America/New_York'):
    """The ZIP for one participant. lo/hi (epoch seconds) limit check-ins, questionnaires and readings;
    None exports everything. Returns (bytes, filename)."""
    cols = get_config().collections
    u = db[cols.USERS].find_one({'uid': uid}, {'_id': 0, 'uid': 1, 'email': 1, 'profile': 1})
    if not u:
        raise ValueError(f'No participant {uid}')
    code = (db[cols.USER_CODE_MAPPINGS].find_one({'uid': uid}, {'_id': 0, 'uid_code': 1}) or {}).get('uid_code')
    has_login = bool(db[participants.LOGINS].find_one({'uid': uid}, {'_id': 1}))

    cq = {'uid': uid}
    if lo is not None:
        cq['started_at'] = {'$gte': _iso(lo), '$lt': _iso(hi)}
    checkins = [_json_safe(c) for c in db[mindguard.COLLECTION].find(cq, {'_id': 0, 'email': 0}).sort('started_at', 1)]
    qq = {'uid': uid}
    if lo is not None:
        qq['completed_at'] = {'$gte': _iso(lo), '$lt': _iso(hi)}
    questionnaires = [_json_safe(x) for x in db[patient_view.QUESTIONNAIRES].find(qq, {'_id': 0}).sort('completed_at', 1)]

    if lo is not None:
        s_lo, s_hi = lo, hi
    else:
        times = [patient_view.epoch_seconds(d.get('timestamp')) for d in
                 db[cols.GARMIN_HR].find({'uid': uid}, {'_id': 0, 'timestamp': 1}).sort('timestamp', 1).limit(1)]
        s_lo, s_hi = (times[0] if times and times[0] else None), datetime.now(timezone.utc).timestamp()
    sleep = patient_view.estimate_sleep(db, uid, s_lo, s_hi, tz) if s_lo else []

    now = datetime.now(timezone.utc)
    rng = {'from': _iso(lo), 'to': _iso(hi)} if lo is not None else 'all'
    bundle = {
        'format': FORMAT, 'version': VERSION, 'exported_at': _iso(now.timestamp()), 'exported_by': by, 'range': rng,
        'participant': {'uid': uid, 'email': u.get('email'), 'watch_login_code': code, 'has_web_login': has_login,
                        'profile': {k: v for k, v in (u.get('profile') or {}).items() if k in PROFILE_KEYS}},
        'checkins': checkins, 'questionnaires': questionnaires, 'sleep_estimates': sleep,
    }

    answer_rows = []
    for c in checkins:
        for a in c.get('answers') or []:
            answer_rows.append([c.get('checkin_id'), c.get('date'), c.get('status'), c.get('started_at'), a.get('at'),
                                a.get('item'), a.get('node'), a.get('question'), a.get('answer'), a.get('cls'),
                                a.get('key') or '', '' if a.get('num') is None else a.get('num')])
    result_rows = []
    for c in checkins:
        for r in patient_view.check_in_results([c]):
            for e in r['entries']:
                result_rows.append([c.get('checkin_id'), c.get('started_at'), r['key'], r['label'], e['value'], e['text'], e['severity']])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('participant.json', json.dumps(bundle, indent=1, ensure_ascii=False))
        z.writestr('checkins.csv', _csv(answer_rows, ['checkin_id', 'date', 'status', 'started_at', 'answered_at', 'item', 'node',
                                                      'question', 'answer', 'cls', 'choice', 'rating']))
        z.writestr('results.csv', _csv(result_rows, ['checkin_id', 'started_at', 'measure', 'label', 'value', 'text', 'severity']))
        z.writestr('sleep_estimates.csv', _csv([[s['kind'], s['start'], s['end'], s['minutes'], s['awake_minutes'], s['avg_hr']]
                                                for s in sleep], ['kind', 'start', 'end', 'minutes', 'awake_minutes', 'avg_heart_rate']))
        for name, coll, cs in RAW + ([BEATS] if beats else []):
            z.writestr(name, _csv(_raw_rows(db, uid, coll, cs, lo, hi), ['time_utc', 'epoch_s'] + [h for h, _ in cs]))
        z.writestr('README.txt', README.format(uid=uid, exported=_iso(now.timestamp()), by=by,
                                               range='all data' if lo is None else f'{_iso(lo)} to {_iso(hi)}'))
    stamp = now.strftime('%Y%m%d-%H%M')
    span = 'all' if lo is None else datetime.fromtimestamp(lo, timezone.utc).strftime('%Y%m%d') + '-' + \
        datetime.fromtimestamp(hi - 1, timezone.utc).strftime('%Y%m%d')
    return buf.getvalue(), f'mindguard_{re.sub(r"[^A-Za-z0-9_-]", "_", uid)}_{span}_{stamp}.zip'


# ---------------------------------------------------------------------------
class ImportError_(ValueError):
    pass


def read_bundle(data, filename=''):
    """participant.json from a ZIP or a .json file, checked for the format."""
    if not data:
        raise ImportError_('The file is empty.')
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportError_('The file is larger than 20 MB. Export a shorter range, or leave out the beat intervals.')
    raw = data
    if data[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.rsplit('/', 1)[-1] == 'participant.json']
                if not names:
                    raise ImportError_('This ZIP has no participant.json. Use a file exported from Mind Guard.')
                info = z.getinfo(names[0])
                if info.file_size > MAX_IMPORT_BYTES:
                    raise ImportError_('participant.json is larger than 20 MB.')
                raw = z.read(info)
        except zipfile.BadZipFile:
            raise ImportError_('Not a readable ZIP file.')
    try:
        b = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError):
        raise ImportError_('Not a Mind Guard participant file (participant.json or the exported ZIP).')
    if not isinstance(b, dict) or b.get('format') != FORMAT:
        raise ImportError_('Not a Mind Guard participant file (participant.json or the exported ZIP).')
    if b.get('version') != VERSION:
        raise ImportError_(f"File version {b.get('version')} is not supported here (expected {VERSION}).")
    return b


def plan_import(db, uid, bundle, overwrite_profile=False):
    """What importing would do, without writing. The same function applies it (see apply_import)."""
    cols = get_config().collections
    user = db[cols.USERS].find_one({'uid': uid}, {'_id': 0, 'email': 1, 'profile': 1})
    if not user:
        raise ImportError_(f'No participant {uid}')
    src = bundle.get('participant') or {}
    plan = {'target_uid': uid, 'file_uid': src.get('uid'), 'exported_at': bundle.get('exported_at'),
            'profile': [], 'checkins': {'new': 0, 'newer': 0, 'unchanged': 0, 'older': 0, 'invalid': 0, 'other_participant': 0},
            'questionnaires': {'new': 0, 'unchanged': 0, 'invalid': 0}, 'errors': [], 'not_imported': []}

    # profile: fill empty fields; replace differing ones only when asked
    cur = user.get('profile') or {}
    wanted = {k: v for k, v in (src.get('profile') or {}).items() if k in PROFILE_KEYS and v not in (None, '')}
    if src.get('email'):
        wanted['email'] = src['email']
    current = dict(cur, email=user.get('email'))
    changes = {}
    for k, v in wanted.items():
        have = current.get(k)
        if have in (None, ''):
            changes[k] = v
            plan['profile'].append({'field': k, 'from': None, 'to': v, 'action': 'add'})
        elif str(have) != str(v):
            plan['profile'].append({'field': k, 'from': have, 'to': v, 'action': 'replace' if overwrite_profile else 'keep'})
            if overwrite_profile:
                changes[k] = v
    if changes:
        try:
            participants.clean_profile({k: v for k, v in changes.items() if k != 'email'})
        except participants.ProfileError as e:
            plan['errors'].append('Profile: ' + str(e))
        if 'email' in changes and db[cols.USERS].find_one(
                {'uid': {'$ne': uid}, 'email': {'$regex': f"^{re.escape(str(changes['email']))}$", '$options': 'i'}}):
            plan['errors'].append('Profile: another participant already has the email ' + str(changes['email']))
    plan['_profile_changes'] = changes

    # check-ins: the same rule as the live intake (a lower seq is an older copy and is ignored)
    good, owners = [], set()
    for c in bundle.get('checkins') or []:
        try:
            clean = mindguard.clean_checkin(dict(c, email=None))
        except mindguard.CheckinError as e:
            plan['checkins']['invalid'] += 1
            if len(plan['errors']) < 5:
                plan['errors'].append(f"Check-in {c.get('checkin_id') if isinstance(c, dict) else '?'}: {e}")
            continue
        clean['email'] = None
        existing = db[mindguard.COLLECTION].find_one({'checkin_id': clean['checkin_id']}, {'seq': 1, 'uid': 1})
        if existing and existing.get('uid') not in (None, uid):
            # The same check-in cannot belong to two people: it stays with the participant who has it.
            plan['checkins']['other_participant'] += 1
            owners.add(existing['uid'])
            continue
        if not existing:
            plan['checkins']['new'] += 1
        elif existing.get('seq', -1) < clean['seq']:
            plan['checkins']['newer'] += 1
        elif existing.get('seq', -1) == clean['seq']:
            plan['checkins']['unchanged'] += 1
            continue
        else:
            plan['checkins']['older'] += 1
            continue
        good.append(clean)
    plan['_checkins'] = good

    # questionnaires: one per scale and completion time
    qs = []
    for q in bundle.get('questionnaires') or []:
        if not isinstance(q, dict) or q.get('scale') not in [s[0] for s in patient_view.SCALES] or \
                not isinstance(q.get('completed_at'), str) or not re.match(r'^\d{4}-\d{2}-\d{2}T', q['completed_at']):
            plan['questionnaires']['invalid'] += 1
            continue
        doc = {'uid': uid, 'scale': q['scale'], 'completed_at': q['completed_at'][:40],
               'total': q.get('total') if patient_view.finite(q.get('total')) else None,
               'source': str(q.get('source') or 'import')[:80],
               'items': [{'question': str(i.get('question') or '')[:500], 'answer': str(i.get('answer') or '')[:500],
                          'score': i.get('score') if patient_view.finite(i.get('score')) else None}
                         for i in (q.get('items') or [])[:60] if isinstance(i, dict)]}
        if db[patient_view.QUESTIONNAIRES].find_one({'uid': uid, 'scale': doc['scale'], 'completed_at': doc['completed_at']}, {'_id': 1}):
            plan['questionnaires']['unchanged'] += 1
        else:
            plan['questionnaires']['new'] += 1
            qs.append(doc)
    plan['_questionnaires'] = qs

    if owners:
        plan['not_imported'].append(f"{plan['checkins']['other_participant']} check-ins already belong to "
                                    f"{', '.join(sorted(owners))}; they stay with them.")
    if bundle.get('sleep_estimates'):
        plan['not_imported'].append('Sleep estimates: worked out again from the watch data on this site.')
    plan['not_imported'].append('Watch and phone readings: they only come from the UbiWell app.')
    return plan


def public_plan(plan):
    return {k: v for k, v in plan.items() if not k.startswith('_')}


def apply_import(db, uid, plan):
    """Write what plan_import found. Refuses when the plan has errors."""
    if plan['errors']:
        raise ImportError_('Fix the problems listed first: ' + '; '.join(plan['errors'][:3]))
    if plan['_profile_changes']:
        participants.update_profile(db, uid, plan['_profile_changes'])
    now = datetime.now(timezone.utc)
    for c in plan['_checkins']:
        existing = db[mindguard.COLLECTION].find_one({'checkin_id': c['checkin_id']}, {'seq': 1})
        if existing and existing.get('seq', -1) >= c['seq']:
            continue                     # a newer copy arrived since the preview
        db[mindguard.COLLECTION].update_one(
            {'checkin_id': c['checkin_id']},
            {'$set': dict(c, uid=uid, source='import', updated_at=now), '$setOnInsert': {'received_at': now}},
            upsert=True)
    if plan['_questionnaires']:
        db[patient_view.QUESTIONNAIRES].insert_many([dict(q) for q in plan['_questionnaires']])
    return public_plan(plan)


def participants_csv(db):
    """The dashboard's participant list as CSV. No passwords, keys or login details."""
    rows = []
    for r in participants.overview(db):
        if r['type'] != 'participant':
            continue
        p = r.get('profile') or {}
        rows.append([r['uid'], r.get('name') or '', r.get('age') or '', r.get('gender') or '', r.get('email') or '',
                     r.get('uid_code') or '', 'yes' if r.get('has_login') else 'no', p.get('injury_date') or '',
                     r.get('last_checkin') or '', _iso(r['last_watch']) if r.get('last_watch') else '',
                     r.get('alexa'), r.get('watch'), '; '.join(w['text'] for w in r['warnings']),
                     '; '.join(('Study team: ' if t['who'] == 'admin' else 'Participant: ') + t['text']
                               for t in r['todos'] if not t.get('optional'))])
    return _csv(rows, ['participant_id', 'name', 'age', 'gender', 'email', 'watch_login_code', 'web_login', 'date_of_injury',
                       'last_checkin_utc', 'last_watch_data_utc', 'mind_guard', 'watch', 'warnings_last_7_days', 'to_do'])
