"""
Mind Guard check-ins: the Alexa skill posts each check-in here, keyed to a participant by email.

The skill sends the whole check-in record after every answer, so a session that is cut off
still leaves everything answered so far. Each post carries a `seq` that only grows within a
check-in; a post with a lower `seq` than the stored one is a late retry and is ignored, so
an older snapshot never overwrites a newer one.

A check-in whose email matches no participant is stored anyway with `uid: None`. Adding the
email to the participant later links it on the next post for that check-in, and nothing a
teen said is lost in the meantime.
"""

import hmac
import logging
import re
from datetime import datetime, timezone

from flask import request
from flask_restful import Resource

from study_framework_core.core.config import get_config
from study_framework_core.core.handlers import get_db

COLLECTION = 'mindguard_checkins'
TIMEZONE = 'America/New_York'  # the skill dates check-ins in this zone (Mind Guard CONFIG.TIMEZONE)
STATUSES = {'in_progress', 'stopped', 'completed', 'ended_safety', 'abandoned'}
FLAG_LEVELS = {'EMERG', 'SAME-DAY', 'SUMMARY'}
MAX_TEXT = 2000
MAX_ITEMS = 300
DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ID_RX = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')


class CheckinError(ValueError):
    pass


def _text(v, limit=MAX_TEXT):
    if v is None:
        return None
    return str(v)[:limit]


def _items(v, name):
    if v is None:
        return []
    if not isinstance(v, list):
        raise CheckinError(f'{name} must be a list')
    return v[:MAX_ITEMS]


def clean_checkin(payload):
    """Validate the posted check-in and keep only known fields, with lengths capped."""
    if not isinstance(payload, dict):
        raise CheckinError('body must be a JSON object')
    checkin_id = payload.get('checkin_id')
    if not isinstance(checkin_id, str) or not ID_RX.match(checkin_id):
        raise CheckinError('checkin_id is required (letters, digits, . _ : -; at most 128)')
    email = payload.get('email')
    if email is not None and (not isinstance(email, str) or '@' not in email or len(email) > 254):
        raise CheckinError('email must be an email address')
    date = payload.get('date')
    if not isinstance(date, str) or not DATE_RX.match(date):
        raise CheckinError('date must be YYYY-MM-DD')
    status = payload.get('status')
    if status not in STATUSES:
        raise CheckinError(f'status must be one of {sorted(STATUSES)}')
    seq = payload.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise CheckinError('seq must be a non-negative integer')

    answers = [{
        'at': _text(a.get('at'), 40),
        'item': _text(a.get('item'), 40),
        'node': _text(a.get('node'), 80),
        'question': _text(a.get('question')),
        'answer': _text(a.get('answer')),
        'cls': _text(a.get('cls'), 40),
    } for a in _items(payload.get('answers'), 'answers') if isinstance(a, dict)]
    flags = [{
        'level': f.get('level') if f.get('level') in FLAG_LEVELS else 'SUMMARY',
        'item': _text(f.get('item'), 40),
        'node': _text(f.get('node'), 80),
        'note': _text(f.get('note')),
        'at': _text(f.get('at'), 40),
    } for f in _items(payload.get('flags'), 'flags') if isinstance(f, dict)]
    summary = [_text(s) for s in _items(payload.get('summary'), 'summary') if isinstance(s, str)]
    asq = payload.get('asq')

    return {
        'checkin_id': checkin_id,
        'email': email.strip().lower() if email else None,
        'date': date,
        'status': status,
        'seq': seq,
        'started_at': _text(payload.get('started_at'), 40),
        'ended_at': _text(payload.get('ended_at'), 40),
        'private': payload.get('private') if isinstance(payload.get('private'), bool) else None,
        'answers': answers,
        'flags': flags,
        'summary': summary,
        'asq': asq if isinstance(asq, dict) else None,
    }


def find_uid_by_email(db, email):
    """Participant whose email matches, ignoring case; None when there is none."""
    if not email:
        return None
    config = get_config()
    user = db[config.collections.USERS].find_one(
        {'email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'}}, {'uid': 1})
    return user['uid'] if user else None


def save_checkin(db, checkin):
    """Insert or update a check-in. Returns (stored, uid); stored is False for a stale retry."""
    uid = find_uid_by_email(db, checkin['email'])
    now = datetime.now(timezone.utc)
    doc = dict(checkin, uid=uid, source='mindguard', updated_at=now)
    existing = db[COLLECTION].find_one({'checkin_id': checkin['checkin_id']}, {'seq': 1})
    if existing and existing.get('seq', -1) > checkin['seq']:
        return False, uid
    db[COLLECTION].update_one(
        {'checkin_id': checkin['checkin_id']},
        {'$set': doc, '$setOnInsert': {'received_at': now}},
        upsert=True,
    )
    return True, uid


def checkins_for_day(db, uid, date):
    """Check-ins for one participant on one YYYY-MM-DD date (the skill's local date), oldest first."""
    return list(db[COLLECTION].find({'uid': uid, 'date': date}, {'_id': 0}).sort('started_at', 1))


def _authorized():
    key = get_config().security.mindguard_key
    header = request.headers.get('Authorization', '')
    if not key or not header.startswith('Bearer '):
        return False
    return hmac.compare_digest(header[len('Bearer '):].encode(), key.encode())


class MindGuardCheckin(Resource):
    """POST /api/v1/mindguard/checkin  (Authorization: Bearer <security.mindguard_key>)"""

    def post(self):
        if not get_config().security.mindguard_key:
            return {'success': False, 'error': 'Mind Guard intake is not configured'}, 503
        if not _authorized():
            return {'success': False, 'error': 'unauthorized'}, 401
        try:
            checkin = clean_checkin(request.get_json(silent=True))
        except CheckinError as e:
            return {'success': False, 'error': str(e)}, 400
        try:
            stored, uid = save_checkin(get_db(), checkin)
        except Exception as e:
            logging.error(f"Mind Guard check-in {checkin['checkin_id']} not saved: {e}")
            return {'success': False, 'error': 'not saved'}, 500
        if uid is None:
            logging.warning(f"Mind Guard check-in {checkin['checkin_id']}: no participant has this email")
        return {'success': True, 'stored': stored, 'matched': uid is not None}, 200
