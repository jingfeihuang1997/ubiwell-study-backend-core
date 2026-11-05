"""
The participant list on the dashboard: who is in the study, when each last sent data, any warning
from their check-ins, and what is still to do before they are fully set up (and who has to do it).

Also the clinical profile the patient page shows (date of injury, history, prescriptions,
recommendations), which the study team enters here because no other system sends it yet.
"""

import re
from datetime import datetime, timedelta, timezone

from study_framework_core.core import mindguard, patient_view, signup
from study_framework_core.core.config import get_config

LOGINS = signup.LOGINS              # web logins for participants: {email, password_hash, uid, created_at}
WARN_DAYS = 7                       # warnings come from check-ins in the last week
STALE_DAYS = 3                      # no watch data for this long is worth a reminder

# Concussion phase from days since the injury (youth concussion care: most recover within 4 weeks).
PHASES = [(14, 'Acute'), (28, 'Subacute'), (None, 'Persistent symptoms window')]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def phase(injury_date, today=None):
    """('Acute', days) from a YYYY-MM-DD date of injury, or None."""
    try:
        d = datetime.strptime(injury_date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    days = ((today or datetime.now(timezone.utc).date()) - d).days
    if days < 0:
        return None
    for top, name in PHASES:
        if top is None or days <= top:
            return {'name': name, 'days': days}


def warnings(db, uid, now=None):
    """Warnings from the last week of check-ins: safety flags the skill raised, then severe or worsening
    answers. Each: {level: high|mid, text, at}."""
    now = now or datetime.now(timezone.utc)
    checkins = mindguard.checkins_between(db, uid, _iso(now - timedelta(days=WARN_DAYS)), _iso(now + timedelta(minutes=1)))
    out = []
    for c in checkins:
        for f in c.get('flags') or []:
            if f.get('level') in ('EMERG', 'SAME-DAY'):
                out.append({'level': 'high' if f['level'] == 'EMERG' else 'mid', 'text': f.get('note') or f['level'],
                            'at': c.get('started_at'), 'kind': f['level']})
    for r in patient_view.check_in_results(checkins):
        L = r['latest']
        if not L:
            continue
        if L['severity'] == 'high' and r['key'] != 'warning_signs':          # the EMERG flag above already says it
            out.append({'level': 'high', 'text': f"{r['label']}: {L['text']}", 'at': L['at'], 'kind': r['key']})
        elif r['trend'] == 'worse' and r['key'] in ('headache', 'symptom_trend', 'mood', 'anxiety', 'fatigue'):
            out.append({'level': 'mid', 'text': f"{r['label']} worse: {L['text']}", 'at': L['at'], 'kind': r['key']})
    out.sort(key=lambda w: (w['level'] != 'high', -(datetime.fromisoformat(w['at'].replace('Z', '+00:00')).timestamp() if w.get('at') else 0)))
    return out


def todos(user, link, code, has_login):
    """What is left to do, and for whom: {who: admin|participant, key, text}."""
    prof = user.get('profile') or {}
    out = []
    if not user.get('email'):
        out.append({'who': 'admin', 'key': 'email', 'text': "Add the participant's email (the Amazon account they use for Alexa)"})
    if not prof.get('name') or not prof.get('age') or not prof.get('gender'):
        out.append({'who': 'admin', 'key': 'profile', 'text': 'Add name, age and gender'})
    if not prof.get('injury_date'):
        out.append({'who': 'admin', 'key': 'clinical', 'text': 'Add the date of injury'})
    if link['alexa'] == 'waiting':
        out.append({'who': 'participant', 'key': 'alexa', 'text': f"Enable Mind Guard on Alexa with {user.get('email')} and do a first check-in"})
    if link['watch'] == 'not_set_up':
        out.append({'who': 'participant', 'key': 'app',
                    'text': f"Sign in to the UbiWell Connect app with code {code}" if code else 'Sign in to the UbiWell Connect app'})
    elif link['watch'] == 'app_only':
        out.append({'who': 'participant', 'key': 'watch', 'text': 'Pair the Garmin watch in the UbiWell Connect app'})
    elif link['watch'] == 'stale':
        out.append({'who': 'participant', 'key': 'sync', 'text': f'Wear the watch and open the app to sync (nothing for {STALE_DAYS}+ days)'})
    if not has_login and signup.PARTICIPANT_WEB_LOGIN:
        out.append({'who': 'participant', 'key': 'login', 'optional': True,
                    'text': f"Optional: sign up on this site with {user.get('email') or 'their email'} to see their own data"})
    return out


def overview(db, now=None):
    """Every participant and every sign-up waiting for review, for the dashboard list."""
    now = now or datetime.now(timezone.utc)
    cols = get_config().collections
    codes = {m['uid']: m.get('uid_code') for m in db[cols.USER_CODE_MAPPINGS].find({}, {'_id': 0, 'uid': 1, 'uid_code': 1})}
    logins = {d['uid'] for d in db[LOGINS].find({}, {'_id': 0, 'uid': 1}) if d.get('uid')}
    rows = []
    for u in db[cols.USERS].find({}, {'_id': 1, 'uid': 1, 'email': 1, 'profile': 1, 'ios_login_time': 1,
                                      'android_login_time': 1, 'ios_last_ping': 1}):
        if not u.get('uid'):
            continue
        link = signup.link_status(db, u, now)
        prof = u.get('profile') or {}
        rows.append({
            'type': 'participant', 'uid': u['uid'], 'email': u.get('email'), 'uid_code': codes.get(u['uid']),
            'name': prof.get('name'), 'age': prof.get('age'), 'gender': prof.get('gender'), 'profile': prof,
            'created_at': u['_id'].generation_time.isoformat() if hasattr(u.get('_id'), 'generation_time') else None,
            'last_checkin': link['last_checkin'], 'last_watch': link['last_watch'], 'alexa': link['alexa'], 'watch': link['watch'],
            'has_login': u['uid'] in logins, 'warnings': warnings(db, u['uid'], now),
            'todos': todos(u, link, codes.get(u['uid']), u['uid'] in logins),
        })
    rows.sort(key=lambda r: (r['name'] or r['uid']).lower())
    reviews = []
    for r in signup.pending(db):
        reviews.append(dict(r, type='request', todos=[{'who': 'admin', 'key': 'review',
                                                       'text': ('Review the sign-up' if not r.get('claim_uid') else
                                                                f"Review: they ask for a web login to {r['claim_uid']}")}]))
    return reviews + rows


# ---------------------------------------------------------------------------
# Profile the study team enters (clinical facts, recommendations)
# ---------------------------------------------------------------------------
DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PSYCH = ('yes', 'no', 'unknown')


class ProfileError(ValueError):
    pass


def clean_profile(data):
    """Only the fields sent are changed; an empty value clears a field. Returns ($set, $unset)."""
    if not isinstance(data, dict):
        raise ProfileError('Form data is missing.')
    sets, unsets = {}, []

    def put(key, value):
        if value in (None, ''):
            unsets.append('profile.' + key)
        else:
            sets['profile.' + key] = value

    if 'name' in data:
        name = str(data['name'] or '').strip()
        if len(name) > 80:
            raise ProfileError('Name: up to 80 characters.')
        put('name', name)
    if 'age' in data:
        age = data['age']
        if age not in (None, ''):
            try:
                age = int(age)
            except (TypeError, ValueError):
                raise ProfileError('Age: a whole number of years.')
            if not 5 <= age <= 120:
                raise ProfileError('Age: between 5 and 120.')
        put('age', age)
    if 'gender' in data:
        g = data['gender'] or ''
        if g and g not in signup.GENDERS:
            raise ProfileError('Choose a gender option.')
        put('gender', g)
    if 'injury_date' in data:
        d = str(data['injury_date'] or '').strip()
        if d:
            if not DATE_RX.match(d):
                raise ProfileError('Date of injury: YYYY-MM-DD.')
            try:
                if datetime.strptime(d, '%Y-%m-%d').date() > datetime.now(timezone.utc).date():
                    raise ProfileError('The date of injury cannot be in the future.')
            except ValueError:
                raise ProfileError('Date of injury: not a real date.')
        put('injury_date', d)
    if 'psych_history' in data:
        p = data['psych_history'] or ''
        if p and p not in PSYCH:
            raise ProfileError('Psychiatric history: yes, no or unknown.')
        put('psych_history', p)
    if 'psych_note' in data:
        put('psych_note', str(data['psych_note'] or '').strip()[:300])
    if 'mh_prescriptions' in data:
        n = data['mh_prescriptions']
        if n not in (None, ''):
            try:
                n = int(n)
            except (TypeError, ValueError):
                raise ProfileError('Mental health prescriptions: a number.')
            if not 0 <= n <= 30:
                raise ProfileError('Mental health prescriptions: 0 to 30.')
        put('mh_prescriptions', n)
    if 'mh_prescription_names' in data:
        put('mh_prescription_names', str(data['mh_prescription_names'] or '').strip()[:300])
    if 'sleep_target_hours' in data:
        h = data['sleep_target_hours']
        if h not in (None, ''):
            try:
                h = float(h)
            except (TypeError, ValueError):
                raise ProfileError('Sleep recommendation: hours a night.')
            if not 4 <= h <= 14:
                raise ProfileError('Sleep recommendation: 4 to 14 hours.')
        put('sleep_target_hours', h)
    if 'steps_target' in data:
        s = data['steps_target']
        if s not in (None, ''):
            try:
                s = int(s)
            except (TypeError, ValueError):
                raise ProfileError('Activity recommendation: steps a day.')
            if not 0 < s <= 50000:
                raise ProfileError('Activity recommendation: 1 to 50,000 steps a day.')
        put('steps_target', s)
    if 'activity_note' in data:
        put('activity_note', str(data['activity_note'] or '').strip()[:300])
    return sets, unsets


def update_profile(db, uid, data):
    """Save the profile fields sent (and the email, which Mind Guard links check-ins by)."""
    cols = get_config().collections
    if not db[cols.USERS].find_one({'uid': uid}, {'_id': 1}):
        raise ProfileError(f'No participant {uid}')
    sets, unsets = clean_profile(data)
    if 'email' in data:
        email = str(data['email'] or '').strip().lower()
        if email:
            if not signup.EMAIL_RX.match(email) or len(email) > 254:
                raise ProfileError('Not an email address.')
            if db[cols.USERS].find_one({'uid': {'$ne': uid}, 'email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'}}):
                raise ProfileError('Another participant already has this email.')
            sets['email'] = email
        else:
            unsets.append('email')
    update = {}
    if sets:
        update['$set'] = sets
    if unsets:
        update['$unset'] = {k: '' for k in unsets}
    if update:
        db[cols.USERS].update_one({'uid': uid}, update)
    if sets.get('email'):
        db[mindguard.COLLECTION].update_many({'uid': None, 'email': sets['email']}, {'$set': {'uid': uid}})
    return db[cols.USERS].find_one({'uid': uid}, {'_id': 0, 'uid': 1, 'email': 1, 'profile': 1})


def verify_participant_login(db, email, password):
    """A participant's web login: their email and the password they chose when signing up."""
    if not signup.PARTICIPANT_WEB_LOGIN:
        return None                     # off during the study: only staff sign in
    email = str(email or '').strip().lower()
    d = db[LOGINS].find_one({'email': email})
    if not d or not signup.check_salted(str(password or ''), d.get('password_hash')):
        return None
    db[LOGINS].update_one({'_id': d['_id']}, {'$set': {'last_login': datetime.now(timezone.utc)}})
    return d['uid']
