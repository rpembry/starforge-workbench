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
from workbench.repository import SQLiteRepository


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
    body = dict(id=identity, host='Synthetic host', display_name='Fixture agent', provider='opencode',
                evidence_state='present', reason='process_observed', summary='DO_NOT_RENDER_TRANSCRIPT',
                observation_sequence=1, observed_at=datetime.now(timezone.utc).isoformat())
    body.update(extra)
    return body


def add_session(api, **extra):
    api.headers['Authorization'] = 'Bearer ' + COLLECTOR
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
    assert 'Sending instructions is not available yet' in detail.text


def test_sessions_view_distinguishes_stale_offline_unknown_and_work(api, repo):
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
        db.execute('UPDATE registered_sessions SET heartbeat_at=? WHERE id=?',
                   ((current - timedelta(seconds=100)).isoformat(), 'registered_session_0003'))
        db.commit()
    listing = api.get('/sessions').text
    assert 'Stale visibility' in listing
    assert 'Offline visibility' in listing
    detail = api.get('/sessions/registered_session_0001').text
    assert 'Synthetic action' in detail and 'Synthetic objective' in detail
    assert 'Less than a minute ago' in detail
    assert api.get('/sessions?limit=1').text.count('<article>') == 1
    assert 'Next' in api.get('/sessions?limit=1').text


def test_unverified_action_reference_is_not_displayed(api):
    action = api.post('/api/actions', json={'title': 'Unlinked synthetic action',
                                            'status': 'accepted', 'execution_mode': 'agent'}).json()
    add_session(api, action_id=action['id'])
    detail = api.get('/sessions/registered_session_0001').text
    assert 'Unlinked synthetic action' not in detail
    assert 'Not associated' in detail


def test_sessions_android_viewport_browser(tmp_path):
    module = os.environ.get('WB_PLAYWRIGHT_MODULE')
    chrome = shutil.which('google-chrome')
    if not module or not chrome:
        pytest.skip('Set WB_PLAYWRIGHT_MODULE and install Chrome for browser viewport check')
    repo = SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as api:
        api.headers['Authorization'] = 'Bearer ' + COLLECTOR
        assert api.post('/api/registered-sessions', json=registration(
            display_name='Synthetic agent with a long but ordinary readable display name')).status_code == 201
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
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
