import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.collector import cycle, publish_registered_sessions, ScanUnavailable
from workbench.main import create_app
from workbench.repository import SQLiteRepository


def test_collector_health_restart_offline_recovery_and_ownership(tmp_path):
    repo = SQLiteRepository(tmp_path/'state'/'workbench.sqlite')
    with TestClient(create_app(repo, Auth({'operator': 'o'*40, 'collector': 'c'*40}))) as api:
        api.headers['Authorization'] = 'Bearer '+'c'*40
        body = dict(source='fixture:launcher', instance_id='first-process', scope='ai-workbench', status='ok', reason='scan_complete', observed_runs=0)
        first = api.post('/api/collectors/heartbeat', json=body).json()
        restarted = api.post('/api/collectors/heartbeat', json={**body, 'instance_id': 'second-process'}).json()
        assert restarted['id'] == first['id']
        api.headers['Authorization'] = 'Bearer '+'o'*40
        assert api.post('/api/collectors/heartbeat', json=body).status_code == 403
        dash = api.get('/api/dashboard').json()
        assert dash['collectors'][0]['health'] == 'ok'
        assert not dash['active'] and not dash['needs_you']['collectors']  # Healthy idle scanner.
        with repo.connection() as db:
            db.execute('UPDATE collectors SET heartbeat_at=?', ((datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat(),))
            db.commit()
        assert api.get('/api/dashboard').json()['needs_you']['collectors'][0]['health'] == 'offline'
        assert 'Agent activity is unknown' in api.get('/').text
        api.headers['Authorization'] = 'Bearer '+'c'*40
        failed = api.post('/api/collectors/heartbeat', json={**body, 'status': 'degraded', 'reason': 'tmux_unavailable'}).json()
        assert failed['last_success_at'] == restarted['last_success_at']
        api.post('/api/collectors/heartbeat', json=body)
        assert SQLiteRepository(repo.path).list('collectors')[0]['status'] == 'ok'


def test_cycle_reports_scan_failure_and_recovers_without_provider_control():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://fixture.example') as api:
        with patch('workbench.collector.collect', side_effect=ScanUnavailable):
            assert cycle(api, 'unused', [], 'first')['reason'] == 'tmux_unavailable'
        with patch('workbench.collector.collect', return_value=[]):
            assert cycle(api, 'unused', [], 'first')['status'] == 'ok'
        with patch('workbench.collector.collect', side_effect=OSError('do not emit source paths')):
            assert cycle(api, 'unused', [], 'first')['reason'] == 'scan_failed'
    assert len(calls) == 3
    assert all(r.url.path == '/api/collectors/heartbeat' for r in calls)
    assert all(b'do not emit' not in r.content for r in calls)


def test_cycle_handles_network_loss_and_retry():
    def broken(request):
        raise httpx.ConnectError('offline', request=request)
    with patch('workbench.collector.collect', return_value=[]):
        with httpx.Client(transport=httpx.MockTransport(broken), base_url='https://fixture.example') as api:
            with pytest.raises(httpx.ConnectError):
                cycle(api, 'unused', [], 'first')
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), base_url='https://fixture.example') as api:
            assert cycle(api, 'unused', [], 'first')['status'] == 'ok'


def test_registered_session_publishing_uses_live_runs_and_protected_bindings(tmp_path):
    cwd = tmp_path/'checkout'; cwd.mkdir()
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text('''contexts:
- id: bound
  title: Bound OpenCode
  cwd: {cwd}
  provider: opencode
  enabled: true
- id: unbound
  title: Unbound Claude
  cwd: {cwd}
  provider: claude
  enabled: true
- id: disabled
  title: Disabled
  cwd: {cwd}
  provider: codex
  enabled: false
'''.format(cwd=cwd))
    launcher = tmp_path/'launcher'; launcher.mkdir(mode=0o700)
    (launcher/'sessions').mkdir(mode=0o700)
    binding = launcher/'sessions/bound.json'
    binding.write_text(json.dumps({'id': 'synthetic-provider-id', 'provider': 'opencode',
                                   'cwd': str(cwd)}))
    binding.chmod(0o600)
    registration_state = tmp_path/'registration-state'/'sessions.json'
    runs = [
        {'id': 'run-bound', 'source_id': 'a'*64, 'context': 'bound', 'provider': 'opencode',
         'action_id': None, 'last_activity_at': '2026-09-18T00:00:00+00:00'},
        {'id': 'run-unbound', 'source_id': 'b'*64, 'context': 'unbound', 'provider': 'claude',
         'action_id': None, 'last_activity_at': None},
        {'id': 'run-disabled', 'source_id': 'c'*64, 'context': 'disabled', 'provider': 'codex',
         'action_id': None, 'last_activity_at': None},
    ]
    payloads = []
    def handle(request):
        payloads.append(request.read().decode())
        return httpx.Response(201, json={})
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://fixture.example') as api:
        assert publish_registered_sessions(api, manifest, [], runs, 'fixture:launcher',
                                           registration_state, launcher) == 2
        assert publish_registered_sessions(api, manifest, [], runs[:2], 'fixture:launcher',
                                           registration_state, launcher) == 2
    records = [json.loads(payload) for payload in payloads]
    assert [item['evidence_state'] for item in records[:2]] == ['present', 'unknown']
    assert records[1]['reason'] == 'exact_binding_missing'
    assert records[0]['id'] == records[2]['id']
    assert records[0]['observation_sequence'] == 1
    assert records[2]['observation_sequence'] == 2
    assert registration_state.stat().st_mode & 0o777 == 0o600
    state_text = registration_state.read_text()
    assert 'synthetic-provider-id' not in state_text
    assert 'run-disabled' not in ''.join(payloads)


def test_registration_rotates_for_process_generation_and_unsafe_binding_is_unknown(tmp_path):
    cwd = tmp_path/'checkout'; cwd.mkdir()
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text(f'''contexts:
- id: opencode
  title: OpenCode
  cwd: {cwd}
  provider: opencode
  enabled: true
''')
    launcher = tmp_path/'launcher'; launcher.mkdir(mode=0o700)
    (launcher/'sessions').mkdir(mode=0o700)
    binding = launcher/'sessions/opencode.json'
    binding.write_text(json.dumps({'id': 'synthetic-id', 'provider': 'opencode',
                                   'cwd': str(cwd)}))
    binding.chmod(0o644)
    state = tmp_path/'state'/'registrations.json'
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.read()))
        return httpx.Response(201, json={})
    run = {'id': 'run-1', 'source_id': 'a'*64, 'context': 'opencode', 'provider': 'opencode',
           'action_id': None, 'last_activity_at': None}
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://fixture.example') as api:
        publish_registered_sessions(api, manifest, [], [run], 'fixture:launcher', state, launcher)
        binding.chmod(0o600)
        publish_registered_sessions(api, manifest, [], [{**run, 'id': 'run-2', 'source_id': 'b'*64}],
                                    'fixture:launcher', state, launcher)
    assert payloads[0]['evidence_state'] == 'unknown'
    assert payloads[1]['evidence_state'] == 'stopped'
    assert payloads[1]['reason'] == 'registration_replaced'
    assert payloads[1]['id'] == payloads[0]['id']
    assert payloads[2]['evidence_state'] == 'present'
    assert payloads[0]['id'] != payloads[2]['id']


def test_cycle_heartbeats_owned_collector_before_registration(tmp_path):
    cwd = tmp_path/'checkout'; cwd.mkdir()
    manifest = tmp_path/'manifest.yaml'
    manifest.write_text(f'''contexts:
- id: opencode
  title: OpenCode
  cwd: {cwd}
  provider: opencode
  enabled: true
''')
    launcher = tmp_path/'launcher'; launcher.mkdir(mode=0o700)
    (launcher/'sessions').mkdir(mode=0o700)
    binding = launcher/'sessions/opencode.json'
    binding.write_text(json.dumps({'id': 'synthetic-id', 'provider': 'opencode',
                                   'cwd': str(cwd)}))
    binding.chmod(0o600)
    run = {'source': 'fixture:tmux', 'source_id': 'a'*64, 'context': 'opencode',
           'provider': 'opencode', 'actor': 'opencode', 'status': 'running',
           'started_at': '2026-09-18T00:00:00+00:00', 'last_activity_at': None,
           'activity_basis': 'synthetic process presence'}
    requests = []
    def handle(request):
        requests.append(request.url.path)
        body = json.loads(request.read())
        if request.url.path == '/api/runs':
            return httpx.Response(201, json={**body, 'id': 'run-id', 'action_id': None})
        return httpx.Response(201 if request.url.path == '/api/registered-sessions' else 200,
                              json={})
    with patch('workbench.collector.collect', return_value=[run]):
        with httpx.Client(transport=httpx.MockTransport(handle),
                          base_url='https://fixture.example') as api:
            health = cycle(api, manifest, [], 'fixture-instance', source='fixture:launcher',
                           registration_state=tmp_path/'registrations'/'state.json',
                           launcher_state=launcher)
    assert health == {'source': 'fixture:launcher', 'instance_id': 'fixture-instance',
                      'scope': 'all configured contexts', 'status': 'ok',
                      'reason': 'scan_complete', 'observed_runs': 1}
    assert requests == ['/api/runs', '/api/collectors/heartbeat',
                        '/api/registered-sessions']


def test_version_one_upgrade_preserves_records(tmp_path):
    import sqlite3
    from pathlib import Path
    from workbench import repository
    state=tmp_path/'state';state.mkdir(mode=0o700)
    path=state/'workbench.sqlite'
    db=sqlite3.connect(path)
    db.executescript((Path(repository.__file__).with_name('migrations')/'001_initial.sql').read_text())
    db.execute('INSERT INTO schema_migrations VALUES (1, ?)', (datetime.now(timezone.utc).isoformat(),))
    db.execute("INSERT INTO objectives VALUES ('existing', 'Preserved', '', NULL, 0, '2026-09-12')")
    db.commit();db.close();path.chmod(0o600)
    repo=SQLiteRepository(path)
    assert repo.get('objectives','existing')['title']=='Preserved'
    assert repo.list('collectors')==[]
