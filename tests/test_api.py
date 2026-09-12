import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository

OPERATOR = 'operator-fixture-'+'x'*32
COLLECTOR = 'collector-fixture-'+'y'*32


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path/'state'/'workbench.sqlite')


@pytest.fixture
def api(repo):
    with TestClient(create_app(repo, Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as client:
        client.headers['Authorization'] = 'Bearer '+OPERATOR
        yield client


def action(api, **extra):
    response = api.post('/api/actions', json={'title': 'Pick up prescription', **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_auth_fail_closed_and_spoofed_headers_rejected(api):
    api.headers.clear()
    assert api.get('/healthz').status_code == 200
    for path in ['/api/dashboard', '/api/actions', '/openapi.json', '/']:
        response = api.get(path, headers={'Cf-Access-Authenticated-User-Email': 'owner@example.com', 'Cf-Access-Jwt-Assertion': 'fake'})
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'unauthorized'
    with pytest.raises(RuntimeError):
        Auth({'operator': 'short', 'collector': 'short'})


def test_human_action_roundtrip_fsm_and_audit(api):
    objective = api.post('/api/objectives', json={'title': 'Health errands'}).json()
    a = action(api, status='accepted', execution_mode='human', objective_id=objective['id'])
    dash = api.get('/api/dashboard').json()
    assert a['id'] in [r['id'] for r in dash['needs_you']['actions']]
    assert a['id'] in [r['id'] for r in dash['next']]
    response = api.post('/api/actions/'+a['id']+'/transitions', json={'version': a['version'], 'transition': 'complete'})
    assert response.status_code == 200
    assert response.json()['status'] == 'done'
    dash = api.get('/api/dashboard').json()
    assert not dash['needs_you']['actions'] and not dash['next']
    assert any(e['summary'] == 'Action done' for e in dash['recent'])
    assert api.post('/api/actions/'+a['id']+'/transitions', json={'version': 2, 'transition': 'start'}).status_code == 409


def test_collector_cannot_commit_promote_or_read_operator_data(api):
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    assert api.post('/api/actions', json={'title': 'Do not infer commitment', 'status': 'accepted'}).status_code == 403
    proposed = action(api, status='proposed', source='collector', source_id='p1')
    assert api.patch('/api/actions/'+proposed['id'], json={'version': 1, 'status': 'accepted'}).status_code == 403
    assert api.post('/api/actions/'+proposed['id']+'/transitions', json={'version': 1, 'transition': 'accept'}).status_code == 403
    assert api.get('/api/dashboard').status_code == 403
    api.headers['Authorization'] = 'Bearer '+OPERATOR
    dash = api.get('/api/dashboard').json()
    assert not dash['next'] and not dash['needs_you']['actions']
    assert dash['suggestions'][0]['id'] == proposed['id']
    assert api.post('/api/actions/'+proposed['id']+'/transitions', json={'version': 1, 'transition': 'accept'}).status_code == 200


def test_observation_is_immutable_idempotent_and_not_an_action(api, repo):
    body = {'summary': 'Schedule manager has been spinning for 2 hours', 'source': 'nl', 'source_id': 'observation-1'}
    first = api.post('/api/events', json=body)
    again = api.post('/api/events', json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()['id'] == again.json()['id']
    assert api.post('/api/events', json={**body, 'summary': 'Different fact'}).status_code == 409
    assert api.get('/api/actions').json()['items'] == []
    assert api.patch('/api/events/'+first.json()['id'], json={'summary': 'Overwrite'}).status_code == 405
    with repo.connection() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE events SET summary=?', ('overwrite',))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM events')


def test_versions_missing_references_and_validation(api):
    a = action(api, status='accepted')
    assert api.patch('/api/actions/'+a['id'], json={'version': 1, 'title': 'Updated'}).status_code == 200
    assert api.patch('/api/actions/'+a['id'], json={'version': 1, 'title': 'Stale'}).json()['error']['code'] == 'version_conflict'
    assert api.post('/api/actions', json={'title': 'Bad reference', 'objective_id': 'missing'}).status_code == 409
    assert api.get('/api/actions/missing').status_code == 404
    assert api.patch('/api/actions/'+a['id'], json={'version': 2, 'title': None}).status_code == 422
    for data in [{'due_date': '2026-02-31'}, {'actor': ''}, {'secret': 'DO_NOT_ECHO'}]:
        response = api.post('/api/actions', json={'title': 'Invalid', **data})
        assert response.status_code == 422
        assert 'DO_NOT_ECHO' not in response.text
    assert api.get('/api/actions?limit=501').status_code == 422
    assert api.post('/api/actions', json={'title': 'cross origin'}, headers={'Origin': 'https://evil.example'}).status_code == 403


def test_runs_heartbeat_staleness_restart_identity_and_relationships(api, repo):
    a = action(api, status='accepted')
    body = dict(source='starforge', source_id='boot:pid:birth', context='ai-workbench', provider='codex', actor='codex', status='running', started_at=datetime.now(timezone.utc).isoformat())
    first = api.post('/api/runs', json=body).json()
    again = api.post('/api/runs', json=body).json()
    assert first['id'] == again['id'] and again['version'] == 2
    a = api.patch('/api/actions/'+a['id'], json={'version': a['version'], 'execution_mode': 'agent'}).json()
    linked = api.post(f'/api/actions/{a["id"]}/runs/{first["id"]}/link', json={
        'action_version': a['version'], 'run_version': again['version']}).json()['run']
    assert api.get('/api/dashboard').json()['active'][0]['action_id'] == a['id']
    assert api.post('/api/runs', json={**body, 'context': 'other'}).status_code == 409
    assert api.post('/api/runs', json={**body, 'started_at': datetime.now(timezone.utc).isoformat()}).status_code == 409
    with repo.connection() as db:
        db.execute('UPDATE runs SET heartbeat_at=?', ((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),))
        db.commit()
    dash = api.get('/api/dashboard').json()
    assert not dash['active'] and dash['stale_runs'][0]['id'] == first['id']
    assert api.patch('/api/runs/'+first['id'], json={'version': linked['version'], 'status': 'stopped'}).status_code == 200


def test_migrations_reopen_permissions_and_artifact(api, repo):
    second = SQLiteRepository(repo.path)
    assert second.list('actions') == []
    assert repo.path.stat().st_mode & 0o777 == 0o600
    assert repo.path.parent.stat().st_mode & 0o777 == 0o700
    assert api.post('/api/artifacts', json={'title': 'Unsafe', 'uri': 'javascript:alert(1)'}).status_code == 422
    assert api.post('/api/artifacts', json={'title': 'Issue', 'uri': 'https://github.com/example/workbench/issues/1'}).status_code == 201


def test_openapi_and_read_view_escape_content(api):
    action(api, title='<script>alert(1)</script>', status='accepted')
    schema = api.get('/openapi.json').json()
    assert '/api/actions/{identity}/transitions' in schema['paths']
    assert 'ActionIn' in schema['components']['schemas']
    assert schema['components']['securitySchemes']['HTTPBearer']['scheme'] == 'bearer'
    assert schema['paths']['/api/actions']['post']['security'] == [{'HTTPBearer': []}]
    response = api.get('/')
    assert response.status_code == 200
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text
    for heading in ['WHAT NEEDS MY ATTENTION?', 'ACTIVE', 'NEXT', 'RECENT']:
        assert heading in response.text


def test_heartbeats_preserve_operator_assignments_and_approval_state(api):
    a = action(api, status='accepted', execution_mode='agent')
    body = dict(source='starforge', source_id='heartbeat-owned', context='ai-workbench', provider='codex', actor='codex', status='running', started_at=datetime.now(timezone.utc).isoformat())
    first = api.post('/api/runs', json=body).json()
    linked = api.post(f'/api/actions/{a["id"]}/runs/{first["id"]}/link', json={
        'action_version': a['version'], 'run_version': first['version']}).json()['run']
    updated = api.patch('/api/runs/'+first['id'], json={'version': linked['version'], 'status': 'approval_needed'})
    assert updated.status_code == 200
    api.headers['Authorization'] = 'Bearer '+COLLECTOR
    heartbeat = api.post('/api/runs', json=body)
    assert heartbeat.status_code == 201
    assert heartbeat.json()['action_id'] == a['id']
    assert heartbeat.json()['status'] == 'approval_needed'
