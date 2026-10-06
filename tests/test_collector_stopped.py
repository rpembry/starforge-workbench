"""Synthetic process evidence for canonical registrations and positive stop transitions."""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.collector import cycle, _registration_state
from workbench.main import create_app
from workbench.repository import SQLiteRepository


def process(root, pid, parent, name, birth):
    path = root / str(pid)
    path.mkdir()
    fields = ['S', str(parent)] + ['0'] * 17 + [str(birth)]
    (path / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))
    (path / 'comm').write_text(name)


def setup(tmp_path):
    proc = tmp_path / 'proc'
    (proc / 'sys/kernel/random').mkdir(parents=True)
    (proc / 'sys/kernel/random/boot_id').write_text('synthetic-boot')
    (proc / 'stat').write_text('btime 1000\n')
    process(proc, 10, 1, 'python', 10)
    process(proc, 20, 10, 'opencode', 20)
    process(proc, 30, 1, 'python', 30)
    process(proc, 40, 30, 'opencode', 40)
    cwd = tmp_path / 'checkout'
    cwd.mkdir()
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(f'''contexts:
- id: synthetic
  title: Synthetic OpenCode
  cwd: {cwd}
  provider: opencode
  enabled: true
''')
    launcher = tmp_path / 'launcher'
    launcher.mkdir(mode=0o700)
    (launcher / 'sessions').mkdir(mode=0o700)
    binding = launcher / 'sessions/synthetic.json'
    binding.write_text(json.dumps({'id': 'ses_synthetic_0001', 'provider': 'opencode',
                                   'cwd': str(cwd)}))
    binding.chmod(0o600)
    state = tmp_path / 'registration-state/sessions.json'
    repo = SQLiteRepository(tmp_path / 'server/workbench.sqlite')
    return proc, manifest, launcher, state, repo


def scan(api, proc, manifest, launcher, state, output=None, selected=()):
    if output is None:
        output = 'sfwb-synthetic|10|0|1100\nsfwb-synthetic|30|0|1100\n'
    result = SimpleNamespace(returncode=0, stdout=output)
    from workbench import collector
    original = collector.collect

    def local_collect(manifest, selected=(), include_scan=False):
        return original(manifest, selected, proc_root=proc, include_scan=include_scan)

    with patch('workbench.collector.collect', side_effect=local_collect), \
         patch('workbench.collector.subprocess.run', return_value=result):
        return cycle(api, manifest, selected, 'synthetic-instance', source='synthetic:launcher',
                     registration_state=state, launcher_state=launcher)


def test_two_panes_keep_one_run_and_one_registration_then_exact_exit_stops(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        ids = []
        for _ in range(3):
            assert scan(api, proc, manifest, launcher, state)['observed_runs'] == 1
            current = repo.list_registered_sessions()[0]
            ids.append(current['id'])
        assert len(set(ids)) == 1
        runs = repo.list('runs')
        assert len(runs) == 1
        assert runs[0]['status'] == 'running'
        assert '2 provider panes observed' in runs[0]['activity_basis']
        saved = _registration_state(state)
        assert saved['version'] == 2
        assert saved['contexts']['synthetic']['process']['pid'] == 20

        # Positive exit evidence overrides an older provider waiting state.
        with repo.connection() as db:
            db.execute("UPDATE runs SET status='waiting' WHERE id=?", (runs[0]['id'],))
            db.commit()
        shutil.rmtree(proc / '20')
        shutil.rmtree(proc / '40')
        scan(api, proc, manifest, launcher, state)
        stopped = repo.list_registered_sessions()[0]
        assert stopped['id'] == ids[0]
        assert stopped['evidence_state'] == 'stopped'
        assert stopped['reason'] == 'process_stopped'
        assert repo.get('runs', runs[0]['id'])['status'] == 'stopped'
        delayed = api.post('/api/runs', json={
            'source': runs[0]['source'], 'source_id': runs[0]['source_id'],
            'context': runs[0]['context'], 'provider': runs[0]['provider'],
            'actor': runs[0]['actor'], 'status': 'running',
            'started_at': runs[0]['started_at'], 'last_activity_at': runs[0]['last_activity_at'],
            'activity_basis': 'delayed synthetic heartbeat'})
        assert delayed.status_code == 409
        assert repo.get('runs', runs[0]['id'])['status'] == 'stopped'
        assert _registration_state(state)['contexts']['synthetic']['stopped'] is True
        assert scan(api, proc, manifest, launcher, state)['observed_runs'] == 0
        assert repo.list_registered_sessions()[0]['observation_sequence'] == stopped['observation_sequence']
        with repo.connection() as db:
            db.execute('UPDATE runs SET heartbeat_at=? WHERE id=?',
                       ((datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat(),
                        runs[0]['id']))
            db.commit()
        assert not repo.dashboard()['stale_runs']


def test_oldest_pane_exit_stops_old_run_while_newer_pane_remains(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        old = repo.list('runs')[0]
        old_id = repo.list_registered_sessions()[0]['id']
        shutil.rmtree(proc / '20')
        assert scan(api, proc, manifest, launcher, state)['observed_runs'] == 1
        runs = repo.list('runs')
        assert len(runs) == 2
        assert repo.get('runs', old['id'])['status'] == 'stopped'
        assert next(run for run in runs if run['id'] != old['id'])['status'] == 'running'
        assert repo.get_registered_session(old_id)['reason'] == 'process_stopped'
        assert repo.get_registered_session(old_id)['evidence_state'] == 'stopped'
        assert _registration_state(state)['contexts']['synthetic']['id'] != old_id
        delayed = api.post('/api/runs', json={
            'source': old['source'], 'source_id': old['source_id'],
            'context': old['context'], 'provider': old['provider'], 'actor': old['actor'],
            'status': 'running', 'started_at': old['started_at'],
            'activity_basis': 'delayed synthetic heartbeat'})
        assert delayed.status_code == 409
        assert repo.get('runs', old['id'])['status'] == 'stopped'


def test_new_canonical_process_does_not_mark_still_live_old_run_stopped(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state, output='sfwb-synthetic|30|0|1100\n')
        old = repo.list('runs')[0]
        old_id = repo.list_registered_sessions()[0]['id']
        scan(api, proc, manifest, launcher, state)
        assert repo.get_registered_session(old_id)['reason'] == 'registration_replaced'
        assert repo.get('runs', old['id'])['status'] == 'running'


def test_changed_boot_stops_prior_generation_without_revival(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        old_id = repo.list_registered_sessions()[0]['id']
        (proc / 'sys/kernel/random/boot_id').write_text('new-synthetic-boot')
        scan(api, proc, manifest, launcher, state, output='')
        assert repo.get_registered_session(old_id)['evidence_state'] == 'stopped'
        scan(api, proc, manifest, launcher, state)
        new_id = repo.list_registered_sessions()[0]['id']
        assert new_id != old_id
        assert repo.get_registered_session(old_id)['evidence_state'] == 'stopped'


def test_failed_or_deselected_scan_never_emits_stop(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        old_id = repo.list_registered_sessions()[0]['id']
        shutil.rmtree(proc / '20')
        shutil.rmtree(proc / '40')
        assert scan(api, proc, manifest, launcher, state, output='', selected=('other',))['status'] == 'ok'
        assert repo.get_registered_session(old_id)['evidence_state'] == 'present'
        manifest.write_text(manifest.read_text().replace('enabled: true', 'enabled: false'))
        scan(api, proc, manifest, launcher, state, output='')
        assert repo.get_registered_session(old_id)['evidence_state'] == 'present'
        manifest.write_text(manifest.read_text().replace('enabled: false', 'enabled: true'))
        with patch('workbench.collector.subprocess.run',
                   return_value=SimpleNamespace(returncode=1, stdout='')):
            assert cycle(api, manifest, (), 'synthetic-instance', source='synthetic:launcher',
                         registration_state=state, launcher_state=launcher)['status'] == 'degraded'
        assert repo.get_registered_session(old_id)['evidence_state'] == 'present'


def test_unreadable_existing_pid_is_unknown_until_absence_is_verified(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        old_id = repo.list_registered_sessions()[0]['id']
        shutil.rmtree(proc / '40')
        (proc / '20/stat').unlink()  # PID directory still exists; no verified exit.
        scan(api, proc, manifest, launcher, state, output='')
        assert repo.get_registered_session(old_id)['evidence_state'] == 'present'
        shutil.rmtree(proc / '20')
        scan(api, proc, manifest, launcher, state, output='')
        assert repo.get_registered_session(old_id)['evidence_state'] == 'stopped'


def test_lost_stop_response_retries_with_reserved_sequence(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        old_id = repo.list_registered_sessions()[0]['id']
        shutil.rmtree(proc / '20')
        shutil.rmtree(proc / '40')

        class LostStopReply:
            lost = False

            def post(self, path, json):
                response = api.post(path, json=json)
                if path == '/api/registered-sessions' and json.get('reason') == 'process_stopped' and not self.lost:
                    self.lost = True
                    raise httpx.ReadTimeout('synthetic lost response', request=httpx.Request('POST', 'https://example.com'))
                return response

        assert scan(LostStopReply(), proc, manifest, launcher, state, output='')['status'] == 'degraded'
        accepted = repo.get_registered_session(old_id)
        reserved = _registration_state(state)['contexts']['synthetic']
        assert accepted['evidence_state'] == 'stopped'
        assert reserved['sequence'] == accepted['observation_sequence']
        assert reserved['stopped'] is False
        scan(api, proc, manifest, launcher, state, output='')
        assert _registration_state(state)['contexts']['synthetic']['stopped'] is True
        assert repo.get_registered_session(old_id)['observation_sequence'] == accepted['observation_sequence'] + 1


def test_v1_registration_state_upgrades_without_changing_identity(tmp_path):
    proc, manifest, launcher, state, repo = setup(tmp_path)
    with TestClient(create_app(repo, Auth({'collector': 'c' * 40, 'operator': 'o' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        scan(api, proc, manifest, launcher, state)
        original = json.loads(state.read_text())
        item = original['contexts']['synthetic']
        identity, sequence = item['id'], item['sequence']
        item.pop('process')
        item.pop('stopped')
        original['version'] = 1
        state.write_text(json.dumps(original))
        state.chmod(0o600)
        scan(api, proc, manifest, launcher, state)
        upgraded = _registration_state(state)
        assert upgraded['version'] == 2
        assert upgraded['contexts']['synthetic']['id'] == identity
        assert upgraded['contexts']['synthetic']['sequence'] == sequence + 1
        assert upgraded['contexts']['synthetic']['process']['pid'] == 20
