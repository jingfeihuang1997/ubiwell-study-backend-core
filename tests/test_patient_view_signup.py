"""
Patient page data (sleep estimate, calendar, check-in results, status) and sign-up review,
against an in-memory MongoDB (mongomock).

    pip install pytest mongomock
    pytest tests/test_patient_view_signup.py
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from flask import Flask
from flask_restful import Api

from study_framework_core.core import config as config_module
from study_framework_core.core import internal_web, mindguard, patient_view, signup
from study_framework_core.core.config import get_config

NY = 'America/New_York'


def ts(s):
    """'2026-10-06T03:00' (UTC) -> epoch seconds."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient()['study_db']
    for mod in (mindguard, internal_web):
        monkeypatch.setattr(mod, 'get_db', lambda: database)
    monkeypatch.setattr(config_module.config.security, 'mindguard_key', 'k')
    database['users'].insert_one({'uid': 'p1', 'email': 'teen@example.com'})
    return database


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(internal_web.__file__)), 'templates'))
    app.secret_key = 'test'
    api = Api(app, prefix='/internal_web')
    for res, url in [(internal_web.PatientPage, '/patient/<user>'), (internal_web.PatientData, '/patient/<user>/data'),
                     (internal_web.PatientCalendar, '/patient/<user>/calendar'), (internal_web.SignupPage, '/signup'),
                     (internal_web.SignupSubmit, '/signup/submit'), (internal_web.SignupRequests, '/user-management/requests'),
                     (internal_web.SignupDecision, '/user-management/requests/<request_id>/<decision>'),
                     (internal_web.ParticipantStatus, '/user-management/status')]:
        api.add_resource(res, url)
    return app.test_client()


def login(client):
    with client.session_transaction() as s:
        s['admin_logged_in'] = True
        s['admin_username'] = 'admin'


# --------------------------------------------------------------------------- sleep
def wear(db, start, end, status='VALID', hr=None, steps=0):
    """One stress reading a minute in [start, end) UTC, optionally a heart rate and steps."""
    t = ts(start)
    while t < ts(end):
        db['garmin_stress'].insert_one({'uid': 'p1', 'timestamp': t, 'status': status, 'heart_rate': 20.0})
        if hr is not None:
            db['garmin_hr'].insert_one({'uid': 'p1', 'timestamp': t, 'heart_rate': float(hr)})
        if steps:
            db['garmin_steps'].insert_one({'uid': 'p1', 'timestamp': t, 'steps': float(steps)})
        t += 60


def test_sleep_night_and_nap_found_desk_time_is_not(db):
    wear(db, '2026-10-06T03:00', '2026-10-06T10:30', hr=55)                  # 23:00-06:30 in New York, still
    wear(db, '2026-10-06T13:00', '2026-10-06T16:00', status='LARGE_MOTION', hr=78, steps=40)   # walking about
    wear(db, '2026-10-06T16:00', '2026-10-06T17:30', hr=76)                  # sitting at a desk, heart rate as by day
    wear(db, '2026-10-06T18:00', '2026-10-06T18:45', hr=64)                  # 14:00 nap, heart rate well below the day
    out = patient_view.estimate_sleep(db, 'p1', ts('2026-10-06T04:00'), ts('2026-10-07T04:00'), NY)
    kinds = [(b['kind'], b['start'][:16], b['minutes']) for b in out]
    assert ('sleep', '2026-10-06T03:00', 450) in kinds
    assert ('nap', '2026-10-06T18:00', 45) in kinds
    assert not any(b['start'].startswith('2026-10-06T16') for b in out)     # the desk block
    assert next(b for b in out if b['kind'] == 'sleep')['avg_hr'] == 55.0


def test_sitting_still_before_bed_is_not_counted_as_sleep(db):
    wear(db, '2026-10-06T12:00', '2026-10-06T21:00', status='LARGE_MOTION', hr=80, steps=30)  # the day
    wear(db, '2026-10-06T22:30', '2026-10-07T03:10', hr=74)                  # evening at a desk: still, heart rate as by day
    wear(db, '2026-10-07T03:10', '2026-10-07T10:50', hr=55)                  # 23:10-06:50 in New York: asleep
    out = patient_view.estimate_sleep(db, 'p1', ts('2026-10-07T04:00'), ts('2026-10-08T04:00'), NY)
    night = [b for b in out if b['kind'] == 'sleep']
    assert len(night) == 1
    assert night[0]['start'][:16] == '2026-10-07T03:10' and night[0]['end'][:16] == '2026-10-07T10:50'


def test_sleep_joins_short_wake_ups_and_splits_long_ones(db):
    wear(db, '2026-10-06T03:00', '2026-10-06T05:00', hr=55)
    wear(db, '2026-10-06T05:00', '2026-10-06T05:10', status='LARGE_MOTION', hr=72)   # up for 10 minutes: same night
    wear(db, '2026-10-06T05:10', '2026-10-06T07:00', hr=55)
    out = patient_view.estimate_sleep(db, 'p1', ts('2026-10-06T00:00'), ts('2026-10-07T00:00'), NY)
    assert len(out) == 1 and out[0]['awake_minutes'] == 10 and out[0]['minutes'] == 230
    db['garmin_stress'].delete_many({})
    db['garmin_hr'].delete_many({})
    wear(db, '2026-10-06T03:00', '2026-10-06T05:00', hr=55)
    wear(db, '2026-10-06T06:00', '2026-10-06T07:00', hr=55)                  # an hour up: not one long sleep
    out = patient_view.estimate_sleep(db, 'p1', ts('2026-10-06T00:00'), ts('2026-10-07T00:00'), NY)
    assert not any(b['kind'] == 'sleep' for b in out)


def test_gaps_in_the_upload_are_not_wake_ups(db):
    t = ts('2026-10-06T03:00')
    while t < ts('2026-10-06T10:30'):                                       # a reading every other minute only
        db['garmin_stress'].insert_one({'uid': 'p1', 'timestamp': t, 'status': 'VALID', 'heart_rate': 20.0})
        db['garmin_hr'].insert_one({'uid': 'p1', 'timestamp': t, 'heart_rate': 55.0})
        t += 120
    out = patient_view.estimate_sleep(db, 'p1', ts('2026-10-06T00:00'), ts('2026-10-07T00:00'), NY)
    assert len(out) == 1 and out[0]['kind'] == 'sleep'
    assert out[0]['awake_minutes'] == 0 and out[0]['minutes'] == 449


def test_off_wrist_is_not_sleep(db):
    wear(db, '2026-10-06T03:00', '2026-10-06T10:30', status='OFF_WRIST')
    assert patient_view.estimate_sleep(db, 'p1', ts('2026-10-06T00:00'), ts('2026-10-07T00:00'), NY) == []


# --------------------------------------------------------------------------- calendar and series
def test_calendar_presence_and_last_activity(db):
    db['garmin_hr'].insert_many([{'uid': 'p1', 'timestamp': ts('2026-10-06T03:20'), 'heart_rate': 60.0},
                                 {'uid': 'p1', 'timestamp': ts('2026-10-06T03:40'), 'heart_rate': float('nan')},
                                 {'uid': 'p1', 'timestamp': ts('2026-10-08T12:05') * 1000, 'heart_rate': 70.0}])
    db['mindguard_checkins'].insert_many([{'uid': 'p1', 'started_at': '2026-10-07T01:34:00.000Z'},
                                          {'uid': 'other', 'started_at': '2026-10-07T02:00:00.000Z'}])
    out = patient_view.calendar_presence(db, 'p1', ts('2026-10-01T00:00'), ts('2026-11-01T00:00'))
    assert out['checkins'] == ['2026-10-07T01:34:00.000Z']
    assert out['watch_hours'] == [ts('2026-10-06T03:00'), ts('2026-10-08T12:00')]
    last = patient_view.last_activity(db, 'p1')
    assert last == {'last_checkin': '2026-10-07T01:34:00.000Z', 'last_watch': ts('2026-10-08T12:05')}


def test_week_series_use_ten_minute_buckets_and_sum_steps(db):
    db['garmin_steps'].insert_many([{'uid': 'p1', 'timestamp': ts('2026-10-06T03:01'), 'steps': 10.0},
                                    {'uid': 'p1', 'timestamp': ts('2026-10-06T03:07'), 'steps': 5.0}])
    out = patient_view.watch_series(db, 'p1', ts('2026-10-01T00:00'), ts('2026-10-08T00:00'))
    steps = next(s for s in out if s['key'] == 'steps')
    assert steps['bucket'] == 600 and steps['points'] == [[ts('2026-10-06T03:00'), 15.0]]
    assert [s['key'] for s in out] == ['heart_rate', 'stress', 'body_battery', 'respiration', 'steps']


# --------------------------------------------------------------------------- results from the log
def checkin(at, answers, flags=(), asq=None, summary=()):
    return {'started_at': at, 'answers': list(answers), 'flags': list(flags), 'asq': asq, 'summary': list(summary)}


def ans(node, item, answer, cls='answer', key=None, num=None):
    return {'node': node, 'item': item, 'answer': answer, 'cls': cls, 'key': key, 'num': num}


def test_results_read_from_the_log():
    c1 = checkin('2026-10-05T01:00:00.000Z', [ans('h_worst', 'symptoms', 'about a four'), ans('m_gate', 'mood', 'mostly good')])
    c2 = checkin('2026-10-06T01:00:00.000Z', [
        ans('d_a', 'danger', 'yes', cls='yes'),
        ans('sx_main', 'symptoms', 'yes a headache', cls='yes'),
        ans('h_worst', 'symptoms', 'seven', num=7),
        ans('sx_trend', 'symptoms', 'it got worse'),
        ans('m_gate', 'mood', 'mostly low', key='low'),
        ans('p_main', 'plan', 'some of it'),
        ans('p_before', 'plan', 'two'), ans('p_after', 'plan', 'six'),
    ], flags=[{'level': 'EMERG', 'item': 'danger', 'note': 'x'}],
        asq={'q1': 'yes', 'q2': 'no', 'q3': 'no', 'q4': 'no'}, summary=['a headache, worst 7 out of 10', '[private] low mood'])
    rows = {r['key']: r for r in patient_view.check_in_results([c1, c2])}
    assert rows['headache']['latest']['value'] == 7 and rows['headache']['trend'] == 'worse'
    assert [e['value'] for e in rows['headache']['entries']] == [7, 4]          # newest first; "four" read from text
    assert rows['warning_signs']['latest']['severity'] == 'high'
    assert rows['symptoms']['latest']['detail'] == 'a headache, worst 7 out of 10'   # private summary left out
    assert rows['symptom_trend']['latest']['text'] == 'Worse'
    assert rows['mood']['latest']['text'] == 'Mostly low' and rows['mood']['trend'] == 'worse'
    assert rows['plan']['latest']['text'] == 'Some of it'
    assert rows['exertion']['latest']['value'] == 4 and rows['exertion']['latest']['severity'] == 'mid'
    assert rows['asq']['latest']['text'] == 'Positive' and rows['asq']['latest']['detail'].startswith('Q1 yes')


def test_results_with_no_check_ins_are_empty_rows():
    rows = patient_view.check_in_results([])
    assert rows and all(r['latest'] is None and r['entries'] == [] for r in rows)
    assert [r['key'] for r in rows] == [m[0] for m in patient_view.MEASURES]      # every measure listed, "not asked" ones too


def test_results_keep_the_questions_and_answers_they_come_from():
    c = checkin('2026-10-06T01:00:00.000Z', [
        dict(ans('m_gate', 'mood', 'kind of low', key='low'), question='How has your mood been overall?'),
        dict(ans('m_why', 'mood_more', 'school stuff'), question='What do you think has been behind it?'),
        dict(ans('z_good', 'closing', 'pizza'), question="What's one thing that went okay today?"),
    ])
    mood = {r['key']: r for r in patient_view.check_in_results([c])}['mood']['latest']
    assert [(x['question'], x['answer']) for x in mood['qa']] == [
        ('How has your mood been overall?', 'kind of low'), ('What do you think has been behind it?', 'school stuff')]


def test_status_dot(db):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert patient_view.status_dot(db, 'p1', now) == 'grey'
    db['mindguard_checkins'].insert_one({'uid': 'p1', 'started_at': '2026-10-07T01:00:00.000Z', 'flags': []})
    assert patient_view.status_dot(db, 'p1', now) == 'green'
    db['mindguard_checkins'].insert_one({'uid': 'p1', 'started_at': '2026-10-06T01:00:00.000Z',
                                         'flags': [{'level': 'SAME-DAY', 'note': 'n'}]})
    assert patient_view.status_dot(db, 'p1', now) == 'red'


def test_mindguard_keeps_choice_key_and_rating():
    c = mindguard.clean_checkin({'checkin_id': 'a', 'date': '2026-10-06', 'status': 'completed', 'seq': 1,
                                 'answers': [{'answer': 'mostly low', 'key': 'low', 'num': None},
                                             {'answer': '7', 'num': 7}, {'answer': 'x', 'num': float('nan')}]})
    assert [(a['key'], a['num']) for a in c['answers']] == [('low', None), (None, 7), (None, None)]


# --------------------------------------------------------------------------- page data endpoints
def test_data_endpoint_carries_sleep_results_and_link(client, db):
    login(client)
    wear(db, '2026-10-06T03:00', '2026-10-06T10:30', hr=55)
    db['users'].update_one({'uid': 'p1'}, {'$set': {'profile': {'name': 'Teen One', 'age': 15, 'gender': 'female'}}})
    r = client.get('/internal_web/patient/p1/data?start=2026-10-06T04:00:00.000Z&end=2026-10-07T04:00:00.000Z&tz=America/New_York')
    d = r.json
    assert r.status_code == 200 and d['participant']['name'] == 'Teen One'
    assert d['participant']['link']['watch'] == 'stale' or d['participant']['link']['watch'] == 'receiving'
    assert d['sleep'][0]['kind'] == 'sleep' and isinstance(d['results'], list)
    week = client.get('/internal_web/patient/p1/data?start=2026-09-30T04:00:00.000Z&end=2026-10-07T04:00:00.000Z&tz=America/New_York')
    assert week.status_code == 200
    cal = client.get('/internal_web/patient/p1/calendar?start=2026-09-28T04:00:00.000Z&end=2026-11-09T05:00:00.000Z')
    assert cal.status_code == 200 and cal.json['watch_hours']


def test_patient_page_lists_participants_with_profile(client, db):
    login(client)
    db['users'].insert_one({'uid': 'p2', 'profile': {'name': 'Alex Kim', 'age': 14, 'gender': 'male'}})
    r = client.get('/internal_web/patient/p1')
    page = r.get_data(as_text=True)
    assert 'Alex Kim' in page and 'p1' in page
    assert r.headers.get('Cache-Control') == 'no-cache'


def test_page_data_needs_login(client):
    assert client.get('/internal_web/patient/p1/data?start=2026-10-06T00:00:00Z&end=2026-10-07T00:00:00Z').status_code == 401
    assert client.get('/internal_web/patient/p1/calendar?start=2026-10-01T00:00:00Z&end=2026-11-01T00:00:00Z').status_code == 401
    assert client.get('/internal_web/user-management/requests').status_code == 401
    assert client.get('/internal_web/user-management/status').status_code == 401
    assert client.post('/internal_web/user-management/requests/x/approve', json={}).status_code == 401


# --------------------------------------------------------------------------- sign-up
PARTICIPANT = {'kind': 'participant', 'name': 'Sam Lee', 'age': '15', 'gender': 'female', 'email': 'Sam@Example.com', 'password': 'sam-secret-1'}
STAFF = {'kind': 'staff', 'name': 'Dr. Wang', 'email': 'pi@example.edu', 'username': 'dwang', 'password': 'long-enough-1'}


@pytest.mark.parametrize('bad', [{'kind': 'robot'}, {'name': ''}, {'email': 'nope'}, {'age': 'x'}, {'age': '200'}, {'gender': 'z'}])
def test_participant_form_is_validated(client, bad):
    r = client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, **bad))
    assert r.status_code == 400 and r.json['error']


@pytest.mark.parametrize('bad', [{'username': 'a b'}, {'password': 'short'}])
def test_staff_form_is_validated(client, bad):
    assert client.post('/internal_web/signup/submit', json=dict(STAFF, **bad)).status_code == 400


def test_signup_is_public_and_stored_pending(client, db):
    r = client.post('/internal_web/signup/submit', json=PARTICIPANT)
    assert r.status_code == 200 and r.json['request']['email'] == 'sam@example.com'
    doc = db[signup.COLLECTION].find_one()
    assert doc['status'] == 'pending' and doc['age'] == 15
    again = client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, email='SAM@example.com'))
    assert again.status_code == 400 and 'already waiting' in again.json['error']
    # the study team already created p1 with this email: the request only asks for a web login to p1
    claim = client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, email='teen@example.com'))
    assert claim.status_code == 200 and claim.json['request']['existing'] is True
    assert db[signup.COLLECTION].find_one({'email': 'teen@example.com'})['claim_uid'] == 'p1'
    assert client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, password='short')).status_code == 400
    assert client.get('/internal_web/signup').status_code == 200


def test_staff_password_is_only_stored_hashed(client, db):
    client.post('/internal_web/signup/submit', json=STAFF)
    doc = db[signup.COLLECTION].find_one({'kind': 'staff'})
    assert 'password' not in doc and doc['password_hash'] == hashlib.sha256(b'long-enough-1').hexdigest()
    login(client)
    listed = client.get('/internal_web/user-management/requests').json['requests']
    assert listed and all('password_hash' not in r for r in listed)


def test_approve_participant_creates_user_profile_and_links_checkins(client, db, monkeypatch):
    made = []

    def fake_create_user(uid, email, study_id):
        made.append((uid, email, study_id))
        db['users'].insert_one({'uid': uid, 'email': email})
        return {'success': True, 'user': {'uid': uid, 'uid_code': study_id + 'ZZ99'}}
    import study_framework_core.core.handlers as handlers
    monkeypatch.setattr(handlers, 'create_user', fake_create_user)
    db['user_code_mappings'].insert_one({'uid': 'p1', 'uid_code': 'B61H06'})
    db['mindguard_checkins'].insert_one({'checkin_id': 'early', 'uid': None, 'email': 'sam@example.com'})
    client.post('/internal_web/signup/submit', json=PARTICIPANT)
    login(client)
    listing = client.get('/internal_web/user-management/requests').json
    assert listing['suggested_study_id'] == 'B6'
    rid = listing['requests'][0]['id']
    r = client.post(f'/internal_web/user-management/requests/{rid}/approve', json={'uid': 'sam01', 'study_id': 'B6'})
    assert r.status_code == 200 and r.json['result']['uid_code'] == 'B6ZZ99'
    assert made == [('sam01', 'sam@example.com', 'B6')]
    assert db['users'].find_one({'uid': 'sam01'})['profile'] == {'name': 'Sam Lee', 'age': 15, 'gender': 'female'}
    assert db['mindguard_checkins'].find_one({'checkin_id': 'early'})['uid'] == 'sam01'
    assert db[signup.COLLECTION].find_one()['status'] == 'approved'
    twice = client.post(f'/internal_web/user-management/requests/{rid}/approve', json={'uid': 'sam02', 'study_id': 'B6'})
    assert twice.status_code == 400


def test_approve_staff_creates_a_working_login(client, db):
    client.post('/internal_web/signup/submit', json=STAFF)
    login(client)
    rid = client.get('/internal_web/user-management/requests').json['requests'][0]['id']
    assert client.post(f'/internal_web/user-management/requests/{rid}/approve', json={}).status_code == 200
    admin = db['admin_users'].find_one({'username': 'dwang'})
    assert admin['password_hash'] == hashlib.sha256(b'long-enough-1').hexdigest() and admin['approved_by'] == 'admin'
    assert 'password_hash' not in db[signup.COLLECTION].find_one()
    dup = client.post('/internal_web/signup/submit', json=dict(STAFF, email='other@example.edu'))
    assert dup.status_code == 400 and 'taken' in dup.json['error']


def test_reject_and_bad_decisions(client, db):
    client.post('/internal_web/signup/submit', json=PARTICIPANT)
    login(client)
    rid = client.get('/internal_web/user-management/requests').json['requests'][0]['id']
    assert client.post(f'/internal_web/user-management/requests/{rid}/maybe', json={}).status_code == 400
    assert client.post('/internal_web/user-management/requests/not-an-id/approve', json={}).status_code == 400
    assert client.post(f'/internal_web/user-management/requests/{rid}/approve', json={'uid': 'bad id!', 'study_id': 'B6'}).status_code == 400
    assert client.post(f'/internal_web/user-management/requests/{rid}/reject', json={}).status_code == 200
    assert client.get('/internal_web/user-management/requests').json['requests'] == []


def test_link_status(db):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert signup.link_status(db, {'uid': 'p1', 'email': 'teen@example.com'}, now)[ 'alexa'] == 'waiting'
    assert signup.link_status(db, {'uid': 'p1'}, now)['alexa'] == 'no_email'
    assert signup.link_status(db, {'uid': 'p1'}, now)['watch'] == 'not_set_up'
    assert signup.link_status(db, {'uid': 'p1', 'ios_login_time': [1]}, now)['watch'] == 'app_only'
    db['garmin_hr'].insert_one({'uid': 'p1', 'timestamp': ts('2026-10-07T12:00'), 'heart_rate': 60.0})
    db['mindguard_checkins'].insert_one({'uid': 'p1', 'started_at': '2026-10-07T01:00:00.000Z'})
    st = signup.link_status(db, {'uid': 'p1'}, now)
    assert st['alexa'] == 'connected' and st['watch'] == 'receiving'
    assert signup.link_status(db, {'uid': 'p1'}, datetime(2026, 10, 20, tzinfo=timezone.utc))['watch'] == 'stale'


def test_pending_count(db):
    assert signup.pending_count(db) == 0
    signup.submit(db, PARTICIPANT)
    assert signup.pending_count(db) == 1


def test_changing_data_is_never_cached(client, db):
    login(client)
    for url in ['/internal_web/patient/p1/data?start=2026-10-06T04:00:00.000Z&end=2026-10-07T04:00:00.000Z',
                '/internal_web/patient/p1/calendar?start=2026-10-01T04:00:00.000Z&end=2026-11-01T04:00:00.000Z',
                '/internal_web/user-management/requests', '/internal_web/user-management/status']:
        assert client.get(url).headers.get('Cache-Control') == 'no-store', url


# --------------------------------------------------------------------------- extra measures, questionnaires
def test_no_reading_values_are_skipped(db):
    t = ts('2026-10-06T12:00')
    db['garmin_hr'].insert_many([{'uid': 'p1', 'timestamp': t, 'heart_rate': -1}, {'uid': 'p1', 'timestamp': t + 60, 'heart_rate': 0},
                                 {'uid': 'p1', 'timestamp': t + 120, 'heart_rate': 70}])
    db['garmin_stress'].insert_one({'uid': 'p1', 'timestamp': t, 'heart_rate': -2, 'body_battery': -1, 'status': 'OFF_WRIST'})
    s = {x['key']: x['points'] for x in patient_view.watch_series(db, 'p1', t - 60, t + 3600)}
    assert [p[1] for p in s['heart_rate']] == [70] and s['stress'] == [] and s['body_battery'] == []


def test_extra_measures(db):
    t = ts('2026-10-06T12:00')
    beats, at = [], t
    for i in range(80):                                   # 800 and 820 ms in turn: RMSSD 20 ms
        v = 800 if i % 2 == 0 else 820
        beats.append({'uid': 'p1', 'timestamp': at, 'bbi': v}); at += v / 1000
    beats.append({'uid': 'p1', 'timestamp': at, 'bbi': 2500})        # an artifact, left out
    db['garmin_ibi'].insert_many(beats)
    db['ios_battery'].insert_many([{'uid': 'p1', 'timestamp': t, 'battery_left': 80}, {'uid': 'p1', 'timestamp': t + 30, 'battery_left': 0.6}])
    for m in range(30):
        db['garmin_stress'].insert_one({'uid': 'p1', 'timestamp': t + m * 60, 'heart_rate': 20, 'status': 'VALID' if m < 20 else 'OFF_WRIST'})
    assert set(patient_view.available_extras(db, 'p1')) == {'hrv', 'worn', 'phone_battery'}
    out = {x['key']: x for x in patient_view.extra_series(db, 'p1', t, t + 3600, ['hrv', 'worn', 'phone_battery'])}
    assert out['hrv']['points'] == [[int(t), 20.0]] and out['hrv']['bucket'] == 300
    assert sum(p[1] for p in out['worn']['points']) == 20
    assert out['phone_battery']['points'] == [[int(t), 70.0]]            # 80 and 60 (a fraction turned into %)
    assert patient_view.extra_series(db, 'p1', t, t + 3600, []) == []


def test_questionnaires_latest_previous_and_bands(db):
    db['questionnaires'].insert_many([
        {'uid': 'p1', 'scale': 'GAD-7', 'completed_at': '2026-10-01T12:00:00.000Z', 'total': 17},
        {'uid': 'p1', 'scale': 'GAD-7', 'completed_at': '2026-10-05T12:00:00.000Z', 'items': [{'question': 'q', 'answer': 'a', 'score': 13}]},
        {'uid': 'p1', 'scale': 'GAD-7', 'completed_at': '2026-10-20T12:00:00.000Z', 'total': 2},      # after the range end
        {'uid': 'p1', 'scale': 'PCSS', 'completed_at': '2026-10-02T12:00:00.000Z', 'total': 30},
    ])
    rows = {r['scale']: r for r in patient_view.questionnaire_results(db, 'p1', '2026-10-10T00:00:00.000Z')}
    assert rows['GAD-7']['latest']['total'] == 13 and rows['GAD-7']['latest']['band'] == 'Moderate'
    assert rows['GAD-7']['previous']['total'] == 17 and rows['GAD-7']['previous']['band'] == 'Severe'
    assert rows['PHQ-9']['latest'] is None
    assert rows['PCSS']['latest']['band'] is None and not rows['PCSS']['has_bands']


def test_data_endpoint_sends_extras_only_when_asked(client, db):
    login(client)
    t = ts('2026-10-06T12:00')
    db['ios_battery'].insert_one({'uid': 'p1', 'timestamp': t, 'battery_left': 50})
    q = '/internal_web/patient/p1/data?start=2026-10-06T04:00:00Z&end=2026-10-07T04:00:00Z&tz=' + NY
    d = client.get(q).get_json()
    assert d['available_extras'] == ['phone_battery'] and not any(s.get('extra') for s in d['series'])
    assert [r['scale'] for r in d['questionnaires']] == ['GAD-7', 'PHQ-9', 'PCSS']
    d = client.get(q + '&extra=phone_battery,nonsense').get_json()
    assert [s['key'] for s in d['series'] if s.get('extra')] == ['phone_battery']


def test_data_endpoint_takes_a_custom_range_of_up_to_31_days(client, db):
    login(client)
    base = '/internal_web/patient/p1/data?tz=' + NY
    assert client.get(base + '&start=2026-10-01T04:00:00Z&end=2026-11-01T04:00:00Z').status_code == 200     # 31 days
    assert client.get(base + '&start=2026-09-01T04:00:00Z&end=2026-11-01T04:00:00Z').status_code == 400


# --------------------------------------------------------------------------- participant web logins, dashboard
@pytest.fixture
def full_client(db):
    """The whole internal web, as deployed (all routes), on the test database."""
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(internal_web.__file__)), 'templates'))
    internal_web.InternalWebBase(app, internal_web.SimpleDashboard())
    app.secret_key = 'test'
    return app.test_client()


def approve_first(client, **body):
    login(client)
    rid = client.get('/internal_web/user-management/requests').json['requests'][0]['id']
    r = client.post(f'/internal_web/user-management/requests/{rid}/approve', json=body)
    with client.session_transaction() as s:
        s.clear()
    return r


def test_claim_gives_an_existing_participant_a_login_and_shows_only_their_data(full_client, db, monkeypatch):
    monkeypatch.setattr(internal_web, 'verify_admin_login', lambda u, p: {'success': False, 'error': 'no'})
    db['users'].insert_one({'uid': 'p2', 'email': 'other@example.com'})
    full_client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, email='teen@example.com'))
    r = approve_first(full_client)
    assert r.status_code == 200 and r.json['result']['uid'] == 'p1' and r.json['result']['existing']
    assert db['users'].count_documents({}) == 2                                   # nobody new created
    assert db['users'].find_one({'uid': 'p1'})['profile'] == {'name': 'Sam Lee', 'age': 15, 'gender': 'female'}
    bad = full_client.post('/internal_web/login-handler', json={'username': 'teen@example.com', 'password': 'wrong-one'})
    assert bad.status_code == 401
    ok = full_client.post('/internal_web/login-handler', json={'username': 'Teen@Example.com', 'password': 'sam-secret-1'})
    assert ok.status_code == 200 and ok.json['redirect'] == '/internal_web/patient/p1'
    q = '?start=2026-10-06T04:00:00Z&end=2026-10-07T04:00:00Z&tz=' + NY
    assert full_client.get('/internal_web/patient/p1/data' + q).status_code == 200
    assert full_client.get('/internal_web/patient/p2/data' + q).status_code == 401              # not their data
    page = full_client.get('/internal_web/patient/p1')
    assert page.status_code == 200 and b'"p2"' not in page.data and b'/patient/p2' not in page.data   # only themself in the list
    assert full_client.get('/internal_web/patient/p2').headers['Location'].endswith('/internal_web/patient/p1')
    for admin_only in ('/internal_web/participants', '/internal_web/user-management/requests'):
        assert full_client.get(admin_only).status_code == 401
    assert full_client.get('/internal_web/dashboard', headers={'Accept': 'text/html'}).status_code == 302
    again = full_client.post('/internal_web/signup/submit', json=dict(PARTICIPANT, email='teen@example.com'))
    assert again.status_code == 400 and 'Sign in' in again.json['error']


def test_dashboard_rows_todos_and_warnings(full_client, db):
    now = datetime.now(timezone.utc)
    db['user_code_mappings'].insert_one({'uid': 'p1', 'uid_code': 'B6AAAA'})
    db['mindguard_checkins'].insert_one({'uid': 'p1', 'checkin_id': 'c1', 'started_at': (now - timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                         'flags': [{'level': 'EMERG', 'item': 'danger', 'note': 'Repeated vomiting'}],
                                         'answers': [{'node': 'h_worst', 'item': 'symptoms', 'answer': '8', 'num': 8}]})
    db['users'].insert_one({'uid': 'p3', 'profile': {'name': 'Ann', 'age': 14, 'gender': 'female', 'injury_date': '2026-10-01'}, 'ios_login_time': [1]})
    full_client.post('/internal_web/signup/submit', json=PARTICIPANT)
    login(full_client)
    rows = full_client.get('/internal_web/participants').json['rows']
    assert rows[0]['type'] == 'request' and rows[0]['todos'][0]['key'] == 'review'
    p1 = next(r for r in rows if r.get('uid') == 'p1')
    keys = {t['key']: t['who'] for t in p1['todos']}
    assert keys == {'profile': 'admin', 'clinical': 'admin', 'app': 'participant', 'login': 'participant'}  # Alexa connected already
    assert 'B6AAAA' in next(t['text'] for t in p1['todos'] if t['key'] == 'app')
    assert [w['text'] for w in p1['warnings']] == ['Repeated vomiting', 'Headache (worst): 8 / 10']
    p3 = next(r for r in rows if r.get('uid') == 'p3')
    assert {t['key'] for t in p3['todos']} == {'email', 'watch', 'login'} and p3['warnings'] == []
    assert full_client.get('/internal_web/dashboard').status_code == 200
    assert full_client.get('/internal_web/user-management').headers['Location'].endswith('/internal_web/dashboard')


def test_profile_endpoint(full_client, db):
    login(full_client)
    db['mindguard_checkins'].insert_one({'checkin_id': 'e', 'uid': None, 'email': 'new@example.com'})
    r = full_client.post('/internal_web/participants/p1/profile', json={
        'name': 'Sam', 'age': '15', 'gender': 'male', 'email': 'New@Example.com', 'injury_date': '2026-10-01',
        'psych_history': 'no', 'mh_prescriptions': '0', 'sleep_target_hours': '9', 'steps_target': '6000'})
    assert r.status_code == 200
    u = db['users'].find_one({'uid': 'p1'})
    assert u['email'] == 'new@example.com' and u['profile']['age'] == 15 and u['profile']['sleep_target_hours'] == 9.0
    assert db['mindguard_checkins'].find_one({'checkin_id': 'e'})['uid'] == 'p1'
    assert full_client.post('/internal_web/participants/p1/profile', json={'steps_target': ''}).status_code == 200
    assert 'steps_target' not in db['users'].find_one({'uid': 'p1'})['profile']
    for bad in ({'age': 'x'}, {'injury_date': '2999-01-01'}, {'psych_history': 'maybe'}, {'email': 'nope'}, {'sleep_target_hours': '30'}):
        assert full_client.post('/internal_web/participants/p1/profile', json=bad).status_code == 400
    assert full_client.post('/internal_web/participants/nobody/profile', json={}).status_code == 400


def test_phase_from_date_of_injury():
    from study_framework_core.core import participants
    today = datetime(2026, 11, 4).date()
    assert participants.phase('2026-10-29', today) == {'name': 'Acute', 'days': 6}
    assert participants.phase('2026-10-10', today)['name'] == 'Subacute'
    assert participants.phase('2026-09-01', today)['name'] == 'Persistent symptoms window'
    assert participants.phase('bad', today) is None and participants.phase('2026-12-01', today) is None


def test_participant_passwords_are_salted():
    a, b = signup.salted_hash("same-password"), signup.salted_hash("same-password")
    assert a != b and a.startswith("pbkdf2_sha256$") and "same-password" not in a
    assert signup.check_salted("same-password", a) and not signup.check_salted("other", a)
    assert not signup.check_salted("x", hashlib.sha256(b"x").hexdigest())          # an unsalted hash is never accepted
