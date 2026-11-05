"""
Sign-up requests from the login page, for participants and for study staff.

A request is stored as a pending profile in signup_requests and shows on the dashboard until an
admin approves or rejects it. Approving a participant creates the UbiWell user (and its watch login
code) with the request's email, which is also what Mind Guard matches check-ins by, and a web login
that shows them only their own data. A participant the study team already created can sign up with
the same email: that request only asks for the web login ("claim_uid"). Approving staff creates an
internal web login. Nothing is created before approval.
"""

import hashlib
import hmac
import os
import re
from datetime import datetime, timezone

from study_framework_core.core.config import get_config

COLLECTION = 'signup_requests'
# Participants' own web login (seeing only their data). Off during the study: the study team works on the
# site with admin accounts, so participants sign up without a password and never sign in. Turning it on
# brings back the password on the sign-up form, sign-in with email, and the request for a login to an
# account the study team created.
PARTICIPANT_WEB_LOGIN = False
LOGINS = 'participant_logins'       # participants' web logins: {email, password_hash, uid}
KINDS = ('participant', 'staff')
GENDERS = ('female', 'male', 'nonbinary', 'other', 'prefer_not_to_say')
EMAIL_RX = re.compile(r'^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$')
USERNAME_RX = re.compile(r'^[A-Za-z0-9._-]{3,40}$')
UID_RX = re.compile(r'^[A-Za-z0-9_-]{2,40}$')
STUDY_ID_RX = re.compile(r'^[A-Za-z0-9]{1,4}$')


class SignupError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _email_rx(email):
    return {'$regex': f'^{re.escape(email)}$', '$options': 'i'}


def password_hash(password):
    # The internal web login (handlers.verify_admin_login) compares an unsalted SHA-256 hex digest,
    # so an approved staff account has to be stored the same way to be able to log in.
    return hashlib.sha256(password.encode()).hexdigest()


PBKDF2_ROUNDS = 200_000


def salted_hash(password):
    """Participants' web passwords: PBKDF2-SHA256 with a random salt per password, so two people with
    the same password get different hashes and a leaked hash cannot be looked up in a precomputed table."""
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f'pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}'


def check_salted(password, stored):
    try:
        algo, rounds, salt, digest = str(stored).split('$')
        if algo != 'pbkdf2_sha256':
            return False
        mine = hashlib.pbkdf2_hmac('sha256', str(password).encode(), bytes.fromhex(salt), int(rounds)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(mine, digest)


def clean_request(data):
    """Validate a sign-up form. Returns the document to store (no plain password)."""
    if not isinstance(data, dict):
        raise SignupError('Form data is missing.')
    kind = data.get('kind')
    if kind not in KINDS:
        raise SignupError('Choose participant or study staff.')
    name = str(data.get('name') or '').strip()
    if not 1 <= len(name) <= 80:
        raise SignupError('Enter your name (up to 80 characters).')
    email = str(data.get('email') or '').strip().lower()
    if not EMAIL_RX.match(email) or len(email) > 254:
        raise SignupError('Enter a valid email address.')
    doc = {'kind': kind, 'name': name, 'email': email, 'status': 'pending', 'created_at': _now()}
    password = str(data.get('password') or '')
    if (kind == 'staff' or PARTICIPANT_WEB_LOGIN) and not 8 <= len(password) <= 128:
        raise SignupError('Password: at least 8 characters.')

    if kind == 'participant':
        try:
            age = int(data.get('age'))
        except (TypeError, ValueError):
            raise SignupError('Enter an age in years.')
        if not 5 <= age <= 120:
            raise SignupError('Enter an age between 5 and 120.')
        gender = data.get('gender')
        if gender not in GENDERS:
            raise SignupError('Choose a gender option.')
        doc.update(age=age, gender=gender)
        if PARTICIPANT_WEB_LOGIN:
            doc['password_hash'] = salted_hash(password)
    else:
        username = str(data.get('username') or '').strip()
        if not USERNAME_RX.match(username):
            raise SignupError('Username: 3 to 40 letters, digits, dots, dashes or underscores.')
        doc.update(username=username, password_hash=password_hash(password),
                   role=str(data.get('role') or '').strip()[:80] or None)
    return doc


def submit(db, data):
    """Store a pending request. Raises SignupError for anything the person should fix."""
    doc = clean_request(data)
    cols = get_config().collections
    if db[COLLECTION].find_one({'kind': doc['kind'], 'email': _email_rx(doc['email']), 'status': 'pending'}):
        raise SignupError('A request with this email is already waiting for review.')
    if doc['kind'] == 'participant':
        existing = db[cols.USERS].find_one({'email': _email_rx(doc['email'])}, {'uid': 1})
        if existing and not PARTICIPANT_WEB_LOGIN:
            raise SignupError('This email is already registered with the study. Contact the study team if you need help.')
        if db[LOGINS].find_one({'email': doc['email']}):
            raise SignupError('This email already has an account. Sign in instead.')
        if existing:
            doc['claim_uid'] = existing['uid']      # the study team created them already: this asks only for a login
    else:
        taken = db['admin_users'].find_one({'username': doc['username']}) or \
            db[COLLECTION].find_one({'kind': 'staff', 'username': doc['username'], 'status': 'pending'})
        if taken:
            raise SignupError('This username is taken. Choose another one.')
    db[COLLECTION].insert_one(doc)
    return {'kind': doc['kind'], 'name': doc['name'], 'email': doc['email'], 'existing': bool(doc.get('claim_uid'))}


def public_view(r):
    """A request as User Management shows it: no password hash."""
    out = {k: r.get(k) for k in ('kind', 'name', 'email', 'age', 'gender', 'username', 'role', 'status', 'uid', 'claim_uid')}
    out['id'] = str(r['_id'])
    for k in ('created_at', 'reviewed_at'):
        v = r.get(k)
        out[k] = v.isoformat() if hasattr(v, 'isoformat') else v
    out['reviewed_by'] = r.get('reviewed_by')
    return out


def pending(db):
    return [public_view(r) for r in db[COLLECTION].find({'status': 'pending'}).sort('created_at', 1)]


def pending_count(db):
    return db[COLLECTION].count_documents({'status': 'pending'})


def suggest_study_id(db):
    """The prefix of the newest watch login code, so approved participants join the same study."""
    m = db[get_config().collections.USER_CODE_MAPPINGS].find_one({}, sort=[('_id', -1)])
    code = (m or {}).get('uid_code') or ''
    return code[:2] if len(code) >= 2 else 'A1'


def _find(db, request_id):
    from bson import ObjectId
    try:
        oid = ObjectId(request_id)
    except Exception:
        raise SignupError('Unknown request.')
    r = db[COLLECTION].find_one({'_id': oid})
    if not r:
        raise SignupError('Unknown request.')
    if r.get('status') != 'pending':
        raise SignupError(f"This request was already {r.get('status')}.")
    return r


def approve(db, request_id, reviewer, uid=None, study_id=None, create_user=None):
    """Approve a pending request. For a participant, `uid` and `study_id` name the new UbiWell
    user; `create_user` is handlers.create_user (passed in so tests can stand it in)."""
    r = _find(db, request_id)
    cols = get_config().collections
    result = {'kind': r['kind'], 'name': r['name'], 'email': r['email']}
    if r['kind'] == 'participant':
        if db[LOGINS].find_one({'email': r['email']}):
            raise SignupError('This email already has a web login.')
        if r.get('claim_uid'):
            # Already a participant: give them the web login, and fill in profile fields that are still empty.
            uid = r['claim_uid']
            user = db[cols.USERS].find_one({'uid': uid}, {'profile': 1, 'email': 1})
            if not user or (user.get('email') or '').lower() != r['email']:
                raise SignupError('The participant with this email has changed since the request; ask them to sign up again.')
            prof = user.get('profile') or {}
            fill = {f'profile.{k}': r[k] for k in ('name', 'age', 'gender') if r.get(k) and not prof.get(k)}
            if fill:
                db[cols.USERS].update_one({'uid': uid}, {'$set': fill})
            code = db[cols.USER_CODE_MAPPINGS].find_one({'uid': uid}, {'uid_code': 1}) or {}
            result.update(uid=uid, uid_code=code.get('uid_code'), existing=True)
        else:
            uid = str(uid or '').strip()
            study_id = str(study_id or '').strip()
            if not UID_RX.match(uid):
                raise SignupError('Participant ID: 2 to 40 letters, digits, dashes or underscores.')
            if not STUDY_ID_RX.match(study_id):
                raise SignupError('Study ID: 1 to 4 letters or digits.')
            if db[cols.USERS].find_one({'email': _email_rx(r['email'])}):
                raise SignupError('Another participant already has this email.')
            made = create_user(uid, r['email'], study_id)
            if not made.get('success'):
                raise SignupError(made.get('error') or 'Could not create the participant.')
            db[cols.USERS].update_one({'uid': uid}, {'$set': {
                'profile': {'name': r['name'], 'age': r['age'], 'gender': r['gender']},
                'signup_request': str(r['_id'])}})
            # Check-ins that came in before the email was registered are linked now.
            db['mindguard_checkins'].update_many({'uid': None, 'email': r['email']}, {'$set': {'uid': uid}})
            result.update(uid=uid, uid_code=made['user']['uid_code'])
        if r.get('password_hash'):
            db[LOGINS].insert_one({'email': r['email'], 'password_hash': r['password_hash'], 'uid': uid,
                                   'created_at': _now(), 'approved_by': reviewer})
    else:
        if db['admin_users'].find_one({'username': r['username']}):
            raise SignupError('This username is already in use.')
        db['admin_users'].insert_one({'username': r['username'], 'password_hash': r['password_hash'],
                                      'created_at': int(_now().timestamp()), 'name': r['name'],
                                      'email': r['email'], 'role': r.get('role'), 'approved_by': reviewer})
        result.update(username=r['username'])
    db[COLLECTION].update_one({'_id': r['_id']}, {'$set': {
        'status': 'approved', 'reviewed_by': reviewer, 'reviewed_at': _now(), 'uid': result.get('uid')},
        '$unset': {'password_hash': ''}})
    return result


def reject(db, request_id, reviewer):
    r = _find(db, request_id)
    db[COLLECTION].update_one({'_id': r['_id']}, {'$set': {
        'status': 'rejected', 'reviewed_by': reviewer, 'reviewed_at': _now()}, '$unset': {'password_hash': ''}})
    return {'kind': r['kind'], 'name': r['name'], 'email': r['email']}


def link_status(db, user, now=None):
    """How far a participant is set up: Alexa (Mind Guard) and the watch."""
    from study_framework_core.core import patient_view
    now = now or datetime.now(timezone.utc)
    uid = user.get('uid')
    last = patient_view.last_activity(db, uid)
    if last['last_checkin']:
        alexa = 'connected'
    elif user.get('email'):
        alexa = 'waiting'          # email on file, no check-in received yet
    else:
        alexa = 'no_email'
    app_login = bool(user.get('ios_login_time') or user.get('android_login_time') or user.get('ios_last_ping'))
    if last['last_watch'] and now.timestamp() - last['last_watch'] <= 3 * 86400:
        watch = 'receiving'
    elif last['last_watch']:
        watch = 'stale'            # data has come in, but not in the last 3 days
    elif app_login:
        watch = 'app_only'         # phone app signed in, no watch data yet
    else:
        watch = 'not_set_up'
    return {'alexa': alexa, 'watch': watch, 'last_checkin': last['last_checkin'], 'last_watch': last['last_watch']}
