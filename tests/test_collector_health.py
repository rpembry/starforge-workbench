from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.collector import cycle, ScanUnavailable
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
