"""Synthetic status data and authorization checks; no live provider access."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository


def client(tmp_path):
    repo = SQLiteRepository(tmp_path / 'private' / 'state.sqlite')
    app = create_app(repo, Auth({'operator': 'o' * 32, 'collector': 'c' * 32}))
    return TestClient(app), repo


def observation(**changes):
    data = dict(provider='ExampleAI', product='Assistant', profile='personal', bucket='default',
                window='weekly', remaining_percent=0, reset_at='2026-11-01T01:30:00-04:00',
                observed_at='2026-10-03T12:00:00+00:00')
    return data | changes


def test_status_is_selective_and_authenticated(tmp_path):
    api, _ = client(tmp_path)
    assert api.get('/status').status_code == 401
    assert api.get('/api/status/allowances').status_code == 401
    auth = {'Authorization': 'Bearer ' + 'o' * 32}
    for widget in ('allowances', 'attention', 'jobs', 'hosts'):
        result = api.get('/api/status/' + widget, headers=auth)
        assert result.status_code == 200, result.text
        assert result.json()['items'] == []
        assert 'checked_at' in result.json()
    assert api.get('/api/status/allowances', headers={'Authorization': 'Bearer ' + 'c' * 32}).status_code == 403
    assert api.get('/api/status/anything', headers=auth).status_code == 422
    assert 'status.js' in api.get('/status', headers=auth).text
    assert api.get('/assets/status.js').status_code == 200


def test_manual_allowances_preserve_windows_profiles_and_observation_time(tmp_path):
    api, _ = client(tmp_path)
    auth = {'Authorization': 'Bearer ' + 'o' * 32}
    first = observation()
    assert api.post('/api/allowances/manual', json=first, headers=auth).status_code == 201
    assert api.post('/api/allowances/manual', json=first, headers=auth).status_code == 201
    assert api.post('/api/allowances/manual', json=observation(window='five-hour', remaining_percent=55), headers=auth).status_code == 201
    assert api.post('/api/allowances/manual', json=observation(profile='other', remaining_percent=None), headers=auth).status_code == 201
    assert api.post('/api/allowances/manual', json=observation(profile='unusual', remaining_percent=101.25), headers=auth).status_code == 201
    older = observation(observed_at='2026-10-02T12:00:00Z', remaining_percent=80)
    assert api.post('/api/allowances/manual', json=older, headers=auth).status_code == 201
    result = api.get('/api/status/allowances', headers=auth).json()
    assert len(result['items']) == 4
    assert next(item for item in result['items'] if item['profile'] == 'unusual')['remaining_percent'] == 101.25
    weekly = next(item for item in result['items'] if item['profile'] == 'personal' and item['window'] == 'weekly')
    assert weekly['remaining_percent'] == 0
    assert weekly['observed_at'] == first['observed_at']
    assert weekly['reset_at'] == '2026-11-01T05:30:00+00:00'
    assert weekly['source_kind'] == 'manual'
    assert weekly['freshness'] == 'stale'
    assert api.post('/api/allowances/manual', json=observation(remaining_percent=1), headers=auth).status_code == 409


def test_manual_import_rejects_ambiguous_or_invalid_values(tmp_path):
    api, _ = client(tmp_path)
    auth = {'Authorization': 'Bearer ' + 'o' * 32}
    for data in (observation(observed_at='2026-10-03T12:00:00'),
                 observation(reset_at='2026-11-01T01:30:00'),
                 observation(remaining_percent='not-a-number'),
                 observation(remaining_percent='NaN'),
                 observation(secret='do-not-echo')):
        response = api.post('/api/allowances/manual', json=data, headers=auth)
        assert response.status_code == 422
        assert 'do-not-echo' not in response.text
    assert api.post('/api/allowances/manual', json=observation()).status_code == 401
    assert api.post('/api/allowances/manual', json=observation(observed_at='2099-01-01T00:00:00Z'), headers=auth).status_code == 422
    assert api.post('/api/allowances/manual', json=observation(),
                    headers={'Authorization': 'Bearer ' + 'c' * 32}).status_code == 403


def test_missing_and_stale_job_evidence(tmp_path):
    api, repo = client(tmp_path)
    auth = {'Authorization': 'Bearer ' + 'o' * 32}
    assert api.get('/api/status/hosts', headers=auth).json()['availability'] == 'unknown'
    start = datetime.now(timezone.utc).isoformat()
    run = api.post('/api/runs', json=dict(source='example', source_id='run-1', context='example-job',
        provider='example', actor='example', status='running', started_at=start), headers=auth)
    assert run.status_code == 201, run.text
    with repo.connection() as db:
        db.execute('UPDATE runs SET heartbeat_at=?', ((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),))
        db.commit()
    jobs = api.get('/api/status/jobs', headers=auth).json()['items']
    assert len(jobs) == 1 and jobs[0]['status'] == 'running' and jobs[0]['freshness'] == 'stale'
