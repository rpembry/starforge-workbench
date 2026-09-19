"""Synthetic worker checks: no live server, provider, or conversation input."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from starforge_workbench.instruction_worker import cycle, load_config
from starforge_workbench.opencode_delivery import DeliveryResult, ResponseEvidence
from workbench.auth import Auth
from workbench.main import create_app
from workbench.repository import SQLiteRepository


REGISTERED = 'registered_synthetic_0001'


def test_worker_unit_preserves_host_tmux_socket_view():
    unit = (Path(__file__).resolve().parents[1] / 'deploy/workbench-instruction-worker.service').read_text()
    assert 'PrivateTmp=false' in unit
    assert 'PrivateTmp=true' not in unit
    assert '--credentials-file %h/.config/starforge-ai-workbench/instruction-collector.json' in unit


def test_rollout_units_keep_one_context_and_loopback_boundary():
    deploy = Path(__file__).resolve().parents[1] / 'deploy'
    publisher = (deploy / 'workbench-opencode-publisher.service').read_text()
    api = (deploy / 'workbench-opencode-api.service').read_text()
    ordinary = (deploy / 'workbench-collector-without-opencode.conf.example').read_text()
    worker = (deploy / 'workbench-instruction-worker.service').read_text()
    config = json.loads((deploy / 'instruction-worker.example.json').read_text())

    assert '--context opencode --interval 30' in publisher
    assert '--source starforge:opencode-instruction' in publisher
    assert '--credentials-file %h/.config/starforge-ai-workbench/instruction-collector.json' in publisher
    assert '--registration-state %h/.local/state/starforge-ai-workbench/instruction-registrations.json' in publisher
    assert '--credentials-file %h/.config/starforge-ai-workbench/instruction-collector.json' in worker
    assert 'instruction-registrations.json' in config['registration_state']
    assert config['manifest'].endswith('/.config/starforge-ai-workbench/workbench.yaml')
    assert config['launcher_state'].endswith('/.local/state/starforge-ai-workbench')
    assert config['enabled'] is False
    assert '--context opencode' not in ordinary
    assert 'ExecStart=' in ordinary
    assert 'serve --hostname 127.0.0.1 --port 4098 --pure' in api
    assert '--hostname 0.0.0.0' not in api


INSTRUCTION = 'instruction_synthetic_001'
SESSION = 'ses_synthetic_session_001'


class Response:
    def __init__(self, status, value=None):
        self.status_code = status
        self.value = value

    def json(self):
        return self.value


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.claim = {'id': INSTRUCTION, 'registered_session_id': REGISTERED,
                      'state': 'claimed', 'text': 'SYNTHETIC INSTRUCTION',
                      'lease_token': 'synthetic_lease_token'}
        self.renew_status = 200
        self.claim_status = 200

    def post(self, path, json):
        self.calls.append((path, json))
        if path == '/api/instructions/claim':
            return Response(self.claim_status, self.claim if self.claim_status == 200 else None)
        if path.endswith('/renew'):
            return Response(self.renew_status, {'state': 'claimed'})
        if path.endswith('/results'):
            return Response(200, {'state': json['outcome']})
        raise AssertionError('Unexpected worker request')


class FakeAdapter:
    def __init__(self, state='received'):
        self.state = state
        self.calls = []
        self.response_evidence = []
        self.marked = []

    def pending_responses(self):
        return self.response_evidence

    def mark_response(self, instruction_id, state):
        self.marked.append((instruction_id, state))

    def deliver(self, instruction_id, session_id, text, lease_token):
        self.calls.append((instruction_id, session_id, text, lease_token))
        return DeliveryResult(self.state, 'synthetic', 'msg_synthetic')


def config(tmp_path, enabled=True):
    root = tmp_path / 'private-state'
    root.mkdir(mode=0o700, exist_ok=True)
    state = root / 'registration.json'
    state.write_text(json.dumps({'version': 1, 'contexts': {'opencode': {
        'id': REGISTERED, 'generation': 'a' * 64, 'sequence': 1,
        'host': 'synthetic', 'display_name': 'Synthetic', 'provider': 'opencode',
        'run_id': 'synthetic-run', 'action_id': None, 'last_activity_at': None,
    }}}))
    state.chmod(0o600)
    return {'enabled': enabled, 'manifest': str(tmp_path / 'manifest.yaml'),
            'registration_state': str(state), 'launcher_state': str(root),
            'delivery_state': str(root), 'opencode_origin': 'http://127.0.0.1:4098',
            'provider_id': 'openai', 'model_id': 'synthetic-model',
            'contexts': ['opencode']}


def test_claim_only_exact_registration_then_report_admission(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter()
    resolver_calls = []

    def resolver(registered_id, *args):
        resolver_calls.append(registered_id)
        return SESSION

    result = cycle(api, config(tmp_path), adapter, resolver)
    assert result == {'eligible': 1, 'claimed': 1, 'reported': 1,
                      'responses_reported': 0, 'ambiguous': 0}
    assert resolver_calls == [REGISTERED, REGISTERED]
    assert adapter.calls == [(INSTRUCTION, SESSION, 'SYNTHETIC INSTRUCTION',
                              'synthetic_lease_token')]
    assert [path for path, _ in api.calls] == [
        '/api/instructions/claim', f'/api/instructions/{INSTRUCTION}/renew',
        f'/api/instructions/{INSTRUCTION}/results']
    assert api.calls[-1][1]['outcome'] == 'received'
    assert api.calls[-1][1]['reason_code'] == 'provider_accepted'
    assert 'SYNTHETIC INSTRUCTION' not in json.dumps(api.calls[-1][1])


def test_kill_switch_and_missing_local_evidence_prevent_claim(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter()
    disabled = config(tmp_path, enabled=False)
    assert cycle(api, disabled, adapter, lambda *args: SESSION)['claimed'] == 0
    assert not api.calls
    assert cycle(api, dict(disabled, enabled=True), adapter, lambda *args: None)['claimed'] == 0
    assert not api.calls and not adapter.calls


def test_completed_response_is_reported_once_before_new_claims(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter()
    api.claim_status = 404
    adapter.response_evidence = [ResponseEvidence(
        INSTRUCTION, 'synthetic_lease_token', 'responded',
        'provider_response_without_error')]

    result = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)

    assert result['responses_reported'] == 1
    assert adapter.marked == [(INSTRUCTION, 'reported')]
    assert api.calls[0] == (f'/api/instructions/{INSTRUCTION}/results', {
        'lease_token': 'synthetic_lease_token', 'outcome': 'responded',
        'reason_code': 'provider_response_without_error'})
    assert api.calls[1][0] == '/api/instructions/claim'


def test_generation_change_after_claim_never_sends(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter()
    calls = 0

    def changed(*args):
        nonlocal calls
        calls += 1
        return SESSION if calls == 1 else None

    result = cycle(api, config(tmp_path), adapter, changed)
    assert result['claimed'] == result['reported'] == 1
    assert not adapter.calls
    assert api.calls[-1][1]['outcome'] == 'failed'
    assert api.calls[-1][1]['reason_code'] == 'session_missing'


def test_renew_failure_and_ambiguous_delivery_never_retry_provider(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter('uncertain')
    api.renew_status = 503
    result = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)
    assert result['ambiguous'] == 1 and not adapter.calls
    api.renew_status = 200
    result = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)
    assert result['reported'] == 1
    assert len(adapter.calls) == 1
    assert api.calls[-1][1]['outcome'] == 'uncertain'
    assert api.calls[-1][1]['reason_code'] == 'delivery_ambiguous'


def test_changed_claim_target_fails_closed(tmp_path):
    api, adapter = FakeAPI(), FakeAdapter()
    api.claim = dict(api.claim, registered_session_id='registered_other_synthetic')
    result = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)
    assert result['ambiguous'] == 1 and result['reported'] == 0
    assert not adapter.calls


def test_private_disabled_configuration_is_accepted(tmp_path):
    content = config(tmp_path, enabled=False)
    path = tmp_path / 'worker.json'
    path.write_text(json.dumps(content))
    path.chmod(0o600)
    assert load_config(path)['enabled'] is False


def test_worker_claim_and_receipt_against_synthetic_server(tmp_path):
    operator = 'operator-synthetic-' + 'o' * 32
    collector = 'collector-synthetic-' + 'c' * 32
    repo = SQLiteRepository(tmp_path / 'database' / 'workbench.sqlite')
    app = create_app(repo, Auth({'operator': operator, 'collector': collector}),
                     instruction_claims_enabled=True)
    with TestClient(app) as api:
        api.headers['Authorization'] = 'Bearer ' + collector
        heartbeat = api.post('/api/collectors/heartbeat', json={
            'source': 'synthetic-collector', 'instance_id': 'synthetic-instance',
            'scope': 'synthetic', 'status': 'ok', 'reason': 'scan_complete'})
        assert heartbeat.status_code == 200
        registration = api.post('/api/registered-sessions', json={
            'id': REGISTERED, 'collector_source': 'synthetic-collector',
            'host': 'synthetic-host', 'display_name': 'Synthetic OpenCode',
            'provider': 'opencode', 'evidence_state': 'present',
            'reason': 'process_observed', 'summary': '', 'observation_sequence': 1,
            'observed_at': datetime.now(timezone.utc).isoformat()})
        assert registration.status_code == 201, registration.text
        other = api.post('/api/registered-sessions', json={
            'id': 'registered_synthetic_0002', 'collector_source': 'synthetic-collector',
            'host': 'synthetic-host', 'display_name': 'Other Synthetic OpenCode',
            'provider': 'opencode', 'evidence_state': 'present',
            'reason': 'process_observed', 'summary': '', 'observation_sequence': 1,
            'observed_at': datetime.now(timezone.utc).isoformat()})
        assert other.status_code == 201, other.text
        api.headers['Authorization'] = 'Bearer ' + operator
        created = api.post('/api/instructions', json={
            'idempotency_key': 'synthetic-key-0001',
            'registered_session_id': REGISTERED, 'text': 'SYNTHETIC INSTRUCTION',
            'expiry_minutes': 15})
        assert created.status_code == 201, created.text
        api.headers['Authorization'] = 'Bearer ' + collector
        adapter = FakeAdapter()
        result = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)
        assert result['claimed'] == result['reported'] == 1
        assert adapter.calls[0][:3] == (
            created.json()['id'], SESSION, 'SYNTHETIC INSTRUCTION')
        assert len(adapter.calls) == 1
        assert adapter.calls[0][3]
        api.headers['Authorization'] = 'Bearer ' + operator
        untouched = api.get('/api/instructions?registered_session_id=registered_synthetic_0002')
        assert untouched.status_code == 200 and untouched.json()['items'] == []
        stored = api.get('/api/instructions/' + created.json()['id'])
        assert stored.status_code == 200
        assert stored.json()['state'] == 'received'
        assert all('SYNTHETIC INSTRUCTION' not in json.dumps(entry)
                   for entry in stored.json()['history'])
        api.headers['Authorization'] = 'Bearer ' + collector
        adapter.response_evidence = [ResponseEvidence(
            created.json()['id'], adapter.calls[0][3], 'responded',
            'provider_response_without_error')]
        follow_up = cycle(api, config(tmp_path), adapter, lambda *args: SESSION)
        assert follow_up['responses_reported'] == 1
        api.headers['Authorization'] = 'Bearer ' + operator
        responded = api.get('/api/instructions/' + created.json()['id']).json()
        assert responded['state'] == 'responded'
        assert [entry['state'] for entry in responded['history']][-2:] == [
            'received', 'responded']
