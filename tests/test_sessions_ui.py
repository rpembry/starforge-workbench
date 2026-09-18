"""Synthetic browser and authorization checks for the read-only Sessions view."""
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import Problem, SQLiteRepository
from workbench.session_views import display_instruction


OPERATOR = 'o' * 40
COLLECTOR = 'c' * 40


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')


@pytest.fixture
def api(repo):
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as client:
        client.headers['Authorization'] = 'Bearer ' + OPERATOR
        yield client


def registration(identity='registered_session_0001', **extra):
    body = dict(id=identity, collector_source='synthetic-collector', host='Synthetic host',
                display_name='Fixture agent', provider='opencode',
                evidence_state='present', reason='process_observed', summary='DO_NOT_RENDER_TRANSCRIPT',
                observation_sequence=1, observed_at=datetime.now(timezone.utc).isoformat())
    body.update(extra)
    return body


def add_session(api, **extra):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post('/api/collectors/heartbeat', json={
        'source': 'synthetic-collector', 'instance_id': 'synthetic-instance', 'scope': 'synthetic host',
        'status': 'ok', 'reason': 'scan_complete'}).status_code == 200
    result = api.post('/api/registered-sessions', json=registration(**extra))
    assert result.status_code == 201, result.text
    api.headers['Authorization'] = 'Bearer ' + OPERATOR
    return result.json()


def test_sessions_page_auth_empty_and_nav(api):
    assert 'href="/sessions"' in api.get('/').text
    assert 'No sessions are registered yet' in api.get('/sessions').text
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.get('/sessions').status_code == 403
    assert api.get('/sessions/registered_session_0001').status_code == 403
    api.headers.clear()
    assert api.get('/sessions').status_code == 401


def test_sessions_page_escapes_labels_and_omits_summary(api, repo):
    add_session(api, display_name='<script>bad</script>')
    listing = api.get('/sessions')
    detail = api.get('/sessions/registered_session_0001')
    assert listing.status_code == detail.status_code == 200
    for page in (listing.text, detail.text):
        assert '&lt;script&gt;bad&lt;/script&gt;' in page
        assert '<script>bad</script>' not in page
        assert 'DO_NOT_RENDER_TRANSCRIPT' not in page
        assert 'Present' in page
        assert 'Registered process observed' in page
        assert 'working' not in page.lower()
    assert 'Queue instruction' in detail.text
    assert '15 minutes (default)' in detail.text
    assert 'Pause, Continue, and Stop are not provided' in detail.text


def test_sessions_view_distinguishes_stale_offline_unknown_and_work(api, repo, monkeypatch):
    objective = api.post('/api/objectives', json={'title': 'Synthetic objective'}).json()
    action = api.post('/api/actions', json={'title': 'Synthetic action', 'objective_id': objective['id'],
                                            'status': 'accepted', 'execution_mode': 'agent'}).json()
    run = api.post('/api/runs', json={'source': 'synthetic', 'source_id': 'synthetic-process-1',
                                     'context': 'Synthetic agent', 'provider': 'opencode', 'actor': 'fixture',
                                     'status': 'running',
                                     'started_at': datetime.now(timezone.utc).isoformat()}).json()
    linked = api.post(f'/api/actions/{action["id"]}/runs/{run["id"]}/link', json={
        'action_version': action['version'], 'run_version': run['version']})
    assert linked.status_code == 200, linked.text
    add_session(api, run_id=run['id'], action_id=action['id'],
                last_activity_at=datetime.now(timezone.utc).isoformat())
    add_session(api, id='registered_session_0002', evidence_state='unknown', reason='no_evidence')
    add_session(api, id='registered_session_0003')
    with repo.connection() as db:
        current = datetime.now(timezone.utc)
        db.execute('UPDATE registered_sessions SET heartbeat_at=? WHERE id=?',
                   ((current - timedelta(seconds=40)).isoformat(), 'registered_session_0002'))
        db.execute('UPDATE collectors SET heartbeat_at=? WHERE source=?',
                   ((current - timedelta(seconds=100)).isoformat(), 'synthetic-collector'))
        db.commit()
    listing = api.get('/sessions').text
    assert 'Stale visibility' not in listing
    assert 'Offline visibility' in listing
    with repo.connection() as db:
        db.execute('UPDATE collectors SET heartbeat_at=? WHERE source=?',
                   (datetime.now(timezone.utc).isoformat(), 'synthetic-collector'))
        db.commit()
    assert 'Stale visibility' in api.get('/sessions').text
    detail = api.get('/sessions/registered_session_0001').text
    assert 'Synthetic action' in detail and 'Synthetic objective' in detail
    assert 'Less than a minute ago' in detail
    assert api.get('/sessions?limit=1').text.count('<article>') == 1
    assert 'Next' in api.get('/sessions?limit=1').text
    original_get = repo.get
    for missing_table in ('runs', 'actions', 'objectives'):
        def missing_link(resource, identity):
            if resource == missing_table:
                raise Problem(404, 'not_found', 'Synthetic missing link')
            return original_get(resource, identity)
        monkeypatch.setattr(repo, 'get', missing_link)
        for path in ('/sessions', '/sessions/registered_session_0001'):
            response = api.get(path)
            assert response.status_code == 200
            if path != '/sessions':
                assert 'Not associated' in response.text


def test_mobile_send_is_idempotent_origin_checked_and_text_safe(api):
    add_session(api)
    detail = api.get('/sessions/' + registration()['id'])
    assert detail.status_code == 200
    key = detail.text.split('name="idempotency_key" value="', 1)[1].split('"', 1)[0]
    form = {'idempotency_key': key, 'text': 'Review <script>DO_NOT_EXECUTE</script> $HOME; then say “ready”.\nDo not merge.',
            'expiry_minutes': '15', 'confirmed': 'yes'}
    path = '/ui/sessions/registered_session_0001/instructions'
    assert api.post(path, data=form, follow_redirects=False).status_code == 403
    assert api.post(path, data=form, headers={'Origin': 'https://evil.example'},
                    follow_redirects=False).status_code == 403
    first = api.post(path, data=form, headers={'Origin': 'http://testserver'}, follow_redirects=False)
    again = api.post(path, data=form, headers={'Origin': 'http://testserver'}, follow_redirects=False)
    assert first.status_code == again.status_code == 303
    assert first.headers['location'] == again.headers['location'] == '/sessions/registered_session_0001?notice=queued'
    listed = api.get('/api/instructions?registered_session_id=registered_session_0001').json()['items']
    assert len(listed) == 1 and listed[0]['idempotency_key'] == key
    page = api.get(first.headers['location']).text
    assert 'Instruction queued' in page
    assert '&lt;script&gt;DO_NOT_EXECUTE&lt;/script&gt;' in page
    assert '<script>DO_NOT_EXECUTE</script>' not in page
    assert 'Provider receipt and requested-work completion have not been established' in page

    changed = 'CHANGED_TEXT_MUST_NOT_BE_ECHOED'
    conflict = api.post(path, data={**form, 'text': changed}, headers={'Origin': 'http://testserver'},
                        follow_redirects=False)
    assert conflict.status_code == 303
    assert conflict.headers['location'].endswith('error=idempotency_conflict')
    assert changed not in conflict.text and changed not in conflict.headers['location']


def test_mobile_send_requires_operator_confirmation_and_controllable_target(api, repo):
    add_session(api, evidence_state='unknown', reason='no_evidence')
    detail = api.get('/sessions/registered_session_0001').text
    assert '<form' not in detail
    assert 'not currently a controllable OpenCode target' in detail
    path = '/ui/sessions/registered_session_0001/instructions'
    body = {'idempotency_key': 'synthetic-ui-key-0001', 'text': 'DO_NOT_ECHO_REJECTED_TEXT',
            'expiry_minutes': '15', 'confirmed': 'yes'}
    rejected = api.post(path, data=body, headers={'Origin': 'http://testserver'}, follow_redirects=False)
    assert rejected.status_code == 303
    assert 'target_uncontrollable' in rejected.headers['location']
    assert body['text'] not in rejected.text and body['text'] not in rejected.headers['location']
    with repo.connection() as db:
        db.execute('UPDATE registered_sessions SET evidence_state=?,reason=?,heartbeat_at=?',
                   ('present', 'process_observed', datetime.now(timezone.utc).isoformat()))
        db.commit()
    unconfirmed = api.post(path, data={**body, 'confirmed': ''}, headers={'Origin': 'http://testserver'},
                           follow_redirects=False)
    assert unconfirmed.status_code == 303
    assert 'invalid_instruction' in unconfirmed.headers['location']
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
    assert api.post(path, data=body, headers={'Origin': 'http://testserver'}).status_code == 403
    api.headers.clear()
    assert api.post(path, data=body, headers={'Origin': 'http://testserver'}).status_code == 401


@pytest.mark.parametrize('state,label', [
    ('queued', 'Queued'), ('claimed', 'Claimed'), ('received', 'Received by provider'),
    ('responded', 'Response observed'), ('failed', 'Delivery failed'),
    ('expired', 'Expired'), ('uncertain', 'Delivery uncertain'),
])
def test_delivery_timeline_has_unambiguous_state_language(state, label):
    stamp = datetime.now(timezone.utc).isoformat()
    item = display_instruction({'state': state, 'created_at': stamp, 'expires_at': stamp, 'history': [{
        'state': state, 'reason_code': 'provider_accepted', 'occurred_at': stamp, 'actor': 'synthetic'}]})
    assert item['state_label'] == label
    if state in {'received', 'responded'}:
        assert 'not evidence' in item['state_explanation'].lower()


def test_detail_renders_every_delivery_state_without_completion_claim(api, repo):
    add_session(api)
    states = ('queued', 'claimed', 'received', 'responded', 'failed', 'expired', 'uncertain')
    for index, state in enumerate(states):
        created = api.post('/api/instructions', json={
            'idempotency_key': f'timeline-key-{index:04d}',
            'registered_session_id': 'registered_session_0001',
            'text': f'Synthetic timeline item {index}', 'expiry_minutes': 60})
        assert created.status_code == 201
        with repo.connection() as db:
            db.execute('UPDATE instructions SET state=?,created_at=? WHERE id=?',
                       (state, (datetime.now(timezone.utc) - timedelta(minutes=2 + index)).isoformat(),
                        created.json()['id']))
            db.commit()
    page = api.get('/sessions/registered_session_0001').text
    for label in ('Queued', 'Claimed', 'Received by provider', 'Response observed',
                  'Delivery failed', 'Expired', 'Delivery uncertain'):
        assert label in page
    assert 'This is not evidence that the requested work completed' in page
    assert 'This is not evidence that the requested work succeeded or completed' in page
    timeline = api.get('/ui/sessions/registered_session_0001/instructions')
    assert timeline.status_code == 200
    assert 'Synthetic timeline item 0' in timeline.text


def test_sessions_android_viewport_browser(tmp_path):
    module = os.environ.get('WB_PLAYWRIGHT_MODULE')
    chrome = shutil.which('google-chrome')
    if not module or not chrome:
        pytest.skip('Set WB_PLAYWRIGHT_MODULE and install Chrome for browser viewport check')
    repo = SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as api:
        api.headers['Authorization'] = 'Bearer ' + COLLECTOR
        assert api.post('/api/collectors/heartbeat', json={
            'source': 'synthetic-collector', 'instance_id': 'synthetic-instance', 'scope': 'synthetic host',
            'status': 'ok', 'reason': 'scan_complete'}).status_code == 200
        assert api.post('/api/registered-sessions', json=registration(
            display_name='Synthetic agent with a long but ordinary readable display name')).status_code == 201
        assert api.post('/api/registered-sessions', json=registration(
            identity='registered_session_0002', display_name='Other disposable agent')).status_code == 201
    app = create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        subprocess.run(['node', str(Path(__file__).with_name('sessions_browser.cjs'))], check=True,
                        env={**os.environ, 'WB_CHROME': chrome, 'WB_TEST_URL': f'http://127.0.0.1:{port}'}, timeout=60)
        selected = repo.list_instructions(session_id='registered_session_0001')
        other = repo.list_instructions(session_id='registered_session_0002')
        assert len(selected) == 1
        assert selected[0]['text'] == 'Synthetic dictation with Unicode \u2713 and $HOME; no merge.'
        assert other == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
