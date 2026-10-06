"""
Mind Guard intake and display, against an in-memory MongoDB (mongomock).

    pip install pytest mongomock
    pytest tests/test_mindguard.py
"""
import os

import mongomock
import pytest
from flask import Flask
from flask_restful import Api

from study_framework_core.core import config as config_module
from study_framework_core.core import mindguard, internal_web
from study_framework_core.core.config import get_config

KEY = 'test-mindguard-key'
EMAIL = 'Teen@Example.com'


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient()['study_db']
    monkeypatch.setattr(mindguard, 'get_db', lambda: database)
    monkeypatch.setattr(internal_web, 'get_db', lambda: database)
    monkeypatch.setattr(config_module.config.security, 'mindguard_key', KEY)
    database[get_config().collections.USERS].insert_one({'uid': 'test_jingfei', 'email': 'teen@example.com'})
    return database


@pytest.fixture
def api_client(db):
    app = Flask(__name__)
    Api(app, prefix='/api/v1').add_resource(mindguard.MindGuardCheckin, '/mindguard/checkin')
    return app.test_client()


@pytest.fixture
def web_client(db, monkeypatch):
    app = Flask(__name__, template_folder=internal_web.__file__.replace('core\\internal_web.py', 'templates').replace('core/internal_web.py', 'templates'))
    app.secret_key = 'test'
    api = Api(app, prefix='/internal_web')
    api.add_resource(internal_web.ViewUserDetail, '/dashboard/view/<user>/<date>')
    api.add_resource(internal_web.ViewDashboardDate, '/dashboard/<date>')
    api.add_resource(internal_web.PatientPage, '/patient/<user>')
    api.add_resource(internal_web.PatientData, '/patient/<user>/data')
    api.add_resource(internal_web.UpdateUserEmail, '/user-management/update-email')
    from study_framework_core.core import processing_scripts
    monkeypatch.setattr(processing_scripts.DataProcessor, '__init__', lambda self: None)
    monkeypatch.setattr(processing_scripts.DataProcessor, 'generate_user_plots',
                        lambda self, uid, date: {'daily_plot': '<p>plot</p>', 'weekly_trends': ''})
    client = app.test_client()
    with client.session_transaction() as s:
        s['admin_logged_in'] = True
        s['admin_username'] = 'admin'
    return client


def checkin(**over):
    body = {
        'checkin_id': 'amzn1-abc.2026-10-06.1791261000',
        'email': EMAIL,
        'date': '2026-10-06',
        'status': 'in_progress',
        'seq': 1,
        'started_at': '2026-10-06T01:15:00.000Z',
        'answers': [{'at': '2026-10-06T01:15:10.000Z', 'item': 'sx', 'node': 'sx_any',
                     'question': 'Any headache today?', 'answer': 'a little <b>bit</b>', 'cls': 'yes'}],
        'flags': [{'level': 'SAME-DAY', 'item': 'mood', 'note': 'Low mood most days', 'at': '2026-10-06T01:16:00Z'}],
        'summary': ['Mild headache'],
    }
    body.update(over)
    return body


def post(client, body, key=KEY):
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    return client.post('/api/v1/mindguard/checkin', json=body, headers=headers)


def test_rejects_missing_or_wrong_key(api_client, db):
    assert post(api_client, checkin(), key=None).status_code == 401
    assert post(api_client, checkin(), key='wrong').status_code == 401
    assert db[mindguard.COLLECTION].count_documents({}) == 0


def test_intake_is_off_without_a_configured_key(api_client, monkeypatch):
    monkeypatch.setattr(config_module.config.security, 'mindguard_key', None)
    assert post(api_client, checkin()).status_code == 503


def test_stores_checkin_matched_by_email_ignoring_case(api_client, db):
    r = post(api_client, checkin())
    assert r.status_code == 200 and r.json['matched'] is True
    doc = db[mindguard.COLLECTION].find_one()
    assert doc['uid'] == 'test_jingfei'
    assert doc['email'] == 'teen@example.com'
    assert doc['source'] == 'mindguard'
    assert doc['answers'][0]['question'] == 'Any headache today?'


def test_later_post_updates_same_checkin_and_stale_retry_is_ignored(api_client, db):
    post(api_client, checkin(seq=1))
    post(api_client, checkin(seq=3, status='completed', answers=checkin()['answers'] * 3))
    r = post(api_client, checkin(seq=2, status='in_progress'))
    assert r.json['stored'] is False
    assert db[mindguard.COLLECTION].count_documents({}) == 1
    doc = db[mindguard.COLLECTION].find_one()
    assert doc['status'] == 'completed' and len(doc['answers']) == 3


def test_unknown_email_is_kept_and_linked_once_email_is_set(api_client, web_client, db):
    r = post(api_client, checkin(email='other@example.com'))
    assert r.json == {'success': True, 'stored': True, 'matched': False}
    assert db[mindguard.COLLECTION].find_one()['uid'] is None
    db[get_config().collections.USERS].insert_one({'uid': 'p2'})
    r = web_client.post('/internal_web/user-management/update-email', json={'uid': 'p2', 'email': 'Other@Example.com'})
    assert r.status_code == 200
    assert db[mindguard.COLLECTION].find_one()['uid'] == 'p2'


def test_email_cannot_be_given_to_two_participants(web_client, db):
    db[get_config().collections.USERS].insert_one({'uid': 'p2'})
    r = web_client.post('/internal_web/user-management/update-email', json={'uid': 'p2', 'email': 'TEEN@example.com'})
    assert r.status_code == 400


@pytest.mark.parametrize('bad', [
    {'checkin_id': ''}, {'checkin_id': 'has space'}, {'date': '10/06/2026'},
    {'status': 'done'}, {'seq': -1}, {'seq': '3'}, {'email': 'not-an-email'}, {'answers': 'x'},
])
def test_rejects_malformed_checkins(api_client, db, bad):
    assert post(api_client, checkin(**bad)).status_code == 400
    assert db[mindguard.COLLECTION].count_documents({}) == 0


def test_long_text_is_capped(api_client, db):
    post(api_client, checkin(answers=[{'question': 'q', 'answer': 'x' * 10000}]))
    assert len(db[mindguard.COLLECTION].find_one()['answers'][0]['answer']) == mindguard.MAX_TEXT


def test_day_page_shows_checkin_escaped_with_flags(api_client, web_client):
    post(api_client, checkin())
    page = web_client.get('/internal_web/dashboard/view/test_jingfei/10-06-26').get_data(as_text=True)
    assert 'Mind Guard Conversation' in page
    assert 'Any headache today?' in page
    assert 'a little &lt;b&gt;bit&lt;/b&gt;' in page
    assert 'Low mood most days' in page and 'SAME-DAY' in page


def test_day_page_shows_asq_answers(api_client, web_client):
    post(api_client, checkin(status='ended_safety', asq={'q1': 'yes', 'q2': 'no', 'q3': 'no', 'q4': 'no'}))
    page = web_client.get('/internal_web/dashboard/view/test_jingfei/10-06-26').get_data(as_text=True)
    assert 'Q1 yes' in page and 'ended safety' in page


def test_day_page_without_checkins_says_so(web_client):
    page = web_client.get('/internal_web/dashboard/view/test_jingfei/10-05-26').get_data(as_text=True)
    assert 'No Mind Guard check-in on this day.' in page


def test_patient_page_renders_for_known_participant(web_client):
    r = web_client.get('/internal_web/patient/test_jingfei?date=2026-10-05')
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and 'pt-body' in page and '"2026-10-05"' in page
    assert 'viewdashboarddate' not in page and '/internal_web/dashboard/10-' not in page   # no jump back to the study dashboard
    assert web_client.get('/internal_web/patient/test_jingfei?date=bad').status_code == 400
    assert web_client.get('/internal_web/patient/nobody').status_code == 404


def day_data(client, start, end):
    return client.get(f'/internal_web/patient/test_jingfei/data?start={start}&end={end}')


def test_day_data_follows_viewer_local_day(api_client, web_client):
    # 23:47 in New York on Oct 5 is 03:47 UTC on Oct 6: it belongs to the New York viewer's Oct 5.
    post(api_client, checkin(checkin_id='late', date='2026-10-05', started_at='2026-10-06T03:47:31.384Z'))
    post(api_client, checkin(checkin_id='next', date='2026-10-06', started_at='2026-10-06T05:34:19.112Z'))
    oct5_ny = day_data(web_client, '2026-10-05T04:00:00.000Z', '2026-10-06T04:00:00.000Z').json
    oct6_ny = day_data(web_client, '2026-10-06T04:00:00.000Z', '2026-10-07T04:00:00.000Z').json
    assert [c['checkin_id'] for c in oct5_ny['checkins']] == ['late']
    assert [c['checkin_id'] for c in oct6_ny['checkins']] == ['next']
    # A viewer in UTC sees both on Oct 6.
    oct6_utc = day_data(web_client, '2026-10-06T00:00:00.000Z', '2026-10-07T00:00:00.000Z').json
    assert [c['checkin_id'] for c in oct6_utc['checkins']] == ['late', 'next']
    assert 'email' not in oct5_ny['checkins'][0]


def test_day_data_includes_participant_and_wearable(web_client, db):
    db['garmin_hr'].insert_many([{'uid': 'test_jingfei', 'timestamp': 1791262000, 'heart_rate': 61.0},
                                 {'uid': 'test_jingfei', 'timestamp': 1791262000 * 1000 + 10, 'heart_rate': 64.0},
                                 {'uid': 'someone_else', 'timestamp': 1791262000, 'heart_rate': 99.0}])
    db['user_code_mappings'].insert_one({'uid': 'test_jingfei', 'uid_code': 'B61H06'})
    d = day_data(web_client, '2026-10-06T04:00:00.000Z', '2026-10-07T04:00:00.000Z').json
    assert d['participant']['uid_code'] == 'B61H06' and d['participant']['email'] == 'teen@example.com'
    hr = next(s for s in d['series'] if s['key'] == 'garmin_hr')
    assert [p[1] for p in hr['points']] == [61.0, 64.0]              # seconds and milliseconds, this user only


@pytest.mark.parametrize('qs', ['', 'start=x&end=y', 'start=2026-10-06T00:00:00&end=2026-10-07T00:00:00',
                                'start=2026-10-06T00:00:00Z&end=2026-10-10T00:00:00Z'])
def test_day_data_rejects_bad_ranges(web_client, qs):
    assert web_client.get('/internal_web/patient/test_jingfei/data?' + qs).status_code == 400


def test_dashboard_lists_every_participant(db):
    from study_framework_core.core.internal_web import dashboard_participants
    db['users'].insert_one({'uid': 'quiet_one'})
    db['daily_summaries'].insert_one({'uid': 'test_jingfei', 'date': 123})
    assert [u['uid'] for u in dashboard_participants(db, get_config(), 123)] == ['quiet_one', 'test_jingfei']


def test_pages_require_login(db):
    app = Flask(__name__)
    app.secret_key = 'test'
    Api(app, prefix='/internal_web').add_resource(internal_web.PatientPage, '/patient/<user>')
    r = app.test_client().get('/internal_web/patient/test_jingfei')
    assert r.status_code == 302 and r.headers['Location'].endswith('/internal_web/login')


def test_data_endpoints_refuse_without_login(db):
    app = Flask(__name__)
    app.secret_key = 'test'
    api = Api(app, prefix='/internal_web')
    api.add_resource(internal_web.PatientData, '/patient/<user>/data')
    api.add_resource(internal_web.UpdateUserEmail, '/user-management/update-email')
    client = app.test_client()
    assert client.get('/internal_web/patient/test_jingfei/data?start=2026-10-06T00:00:00Z&end=2026-10-07T00:00:00Z').status_code == 401
    assert client.post('/internal_web/user-management/update-email', json={'uid': 'test_jingfei', 'email': 'x@y.z'}).status_code == 401


def test_day_data_skips_readings_the_watch_could_not_take(web_client, db):
    # The watch logs stress and respiration as NaN when it has no valid reading; NaN is not JSON.
    db['garmin_stress'].insert_many([{'uid': 'test_jingfei', 'timestamp': 1791262008, 'heart_rate': float('nan')},
                                     {'uid': 'test_jingfei', 'timestamp': 1791262068, 'heart_rate': 31.0},
                                     {'uid': 'test_jingfei', 'timestamp': 1791262128, 'heart_rate': float('inf')}])
    r = day_data(web_client, '2026-10-06T04:00:00.000Z', '2026-10-07T04:00:00.000Z')
    import json
    json.loads(r.get_data(as_text=True))          # strict JSON: would fail on NaN
    stress = next(s for s in r.json['series'] if s['key'] == 'garmin_stress')
    assert stress['points'] == [[1791262068, 31.0]]
