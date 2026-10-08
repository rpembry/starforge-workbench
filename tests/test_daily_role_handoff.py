"""Synthetic daily-role and exact handoff contract tests."""
import copy
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
import httpx
from mcp import Client

from starforge_workbench import cli
from starforge_workbench.role_policy import instructions
from workbench.agent_handoff import Handoff
from workbench.mcp_server import build_server


ROOT = Path(__file__).resolve().parents[1]
SESSION = 'registered_synthetic_0001'
KEY = 'synthetic-handoff-key-0001'
TEXT = 'Please review the synthetic report.'


def context(tmp_path):
    value = copy.deepcopy(yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())['contexts'][0])
    value['cwd'] = str(tmp_path)
    return value


def test_daily_role_is_opt_in_and_preserves_existing_binding_and_input(tmp_path):
    regular = context(tmp_path)
    daily = {**regular, 'role': 'daily-interface'}
    assert cli.fingerprint(daily) == cli.fingerprint(regular)
    assert cli.provider_argv(regular, 'new') == cli.provider_argv(regular, 'new')
    new = cli.provider_argv(daily, 'new')
    with patch.object(cli, 'saved_session', return_value={'id': 'synthetic-conversation'}):
        resume = cli.provider_argv(daily, 'resume')
    assert resume[-2:] == ['resume', 'synthetic-conversation']
    assert new == resume[:-2]
    assert '-c' in new and any('developer_instructions=' in value for value in new)
    assert 'daily Workbench operator interface' in instructions('daily-interface')
    assert 'Do not perform Workbench development' in instructions('daily-interface')
    assert 'substantial research' in instructions('daily-interface')
    assert 'typed Workbench MCP tools first' in instructions('daily-interface')
    assert 'copyable handoff' in instructions('daily-interface')
    with patch.object(cli, 'saved_session', return_value=None):
        with pytest.raises(ValueError, match='exact saved conversation'):
            cli.provider_argv(daily, 'resume')


def test_role_validation_keeps_other_contexts_unmodified(tmp_path):
    manifest = yaml.safe_load((ROOT/'config/workbench.example.yaml').read_text())
    manifest['contexts'][0]['role'] = 'daily-interface'
    path = tmp_path/'manifest.yaml'
    path.write_text(yaml.safe_dump(manifest))
    loaded = cli.load(path)['contexts']
    assert loaded[0]['role'] == 'daily-interface'
    assert all('role' not in item for item in loaded[1:])
    manifest['contexts'][0]['provider'] = 'claude'
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match='Codex conversation'):
        cli.load(path)


def service(tmp_path, provider='opencode', verify=lambda identity: True):
    directory = tmp_path/'private'
    directory.mkdir(mode=0o700, parents=True)
    state = directory/'registrations.json'
    state.write_text(json.dumps({'version': 1, 'contexts': {'target': {
        'id': SESSION, 'generation': 'a'*64, 'sequence': 1,
        'host': 'synthetic-host', 'display_name': 'Synthetic target',
        'provider': 'opencode', 'run_id': 'synthetic-run', 'action_id': None,
        'last_activity_at': None}}}))
    state.chmod(0o600)
    calls = []
    attempts = {}
    remote = {'id': SESSION, 'provider': 'opencode', 'visibility': 'fresh',
              'evidence_state': 'present'}
    def request(method, path, payload=None):
        calls.append((method, path))
        if path == '/api/registered-sessions/'+SESSION:
            return remote
        if path.startswith('/api/instructions/by-key/'):
            key = path.removeprefix('/api/instructions/by-key/')
            if key not in attempts:
                raise LookupError('missing')
            return attempts[key]
        if path == '/api/instructions':
            row = {'id': 'instruction_synthetic_0001', 'registered_session_id': SESSION,
                   'text': payload['text'], 'expiry_minutes': 15, 'state': 'queued',
                   'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'}
            attempts[payload['idempotency_key']] = row
            return row
        raise AssertionError(path)
    work = Handoff([{'id': 'target', 'provider': provider, 'enabled': True}], state, request, verify)
    return work, calls, attempts, remote


def test_exact_handoff_reuses_queue_key_and_reports_truthful_state(tmp_path):
    work, calls, attempts, remote = service(tmp_path)
    assert work.target('target') == {'outcome': 'ready', 'context_id': 'target', 'session_id': SESSION}
    first = work.send('target', SESSION, TEXT, KEY)
    assert first['outcome'] == 'accepted' and first['state'] == 'queued'
    second = work.send('target', SESSION, TEXT, KEY)
    assert second == first
    assert sum(path == '/api/instructions' for _, path in calls) == 1
    attempts[KEY]['state'] = 'received'
    assert work.send('target', SESSION, TEXT, KEY)['outcome'] == 'received'
    assert work.send('target', SESSION, 'Different text', KEY)['outcome'] == 'denied'
    remote['visibility'] = 'offline'
    assert work.send('target', SESSION, TEXT, 'another-synthetic-key-0001')['outcome'] == 'copyable'


def test_unsupported_missing_changed_and_uncertain_targets_fail_closed(tmp_path):
    work, calls, attempts, remote = service(tmp_path, provider='codex')
    assert work.send('target', SESSION, TEXT, KEY) == {
        'outcome': 'copyable', 'reason': 'provider_delivery_unsupported',
        'context_id': 'target', 'handoff': TEXT}
    assert calls == []
    work.contexts = []
    assert work.target('target')['reason'] == 'missing_or_ambiguous_context'
    work, calls, attempts, remote = service(tmp_path/'changed', verify=lambda identity: False)
    assert work.send('target', SESSION, TEXT, KEY)['reason'] == 'target_changed_or_unavailable'
    assert all(path != '/api/instructions' for _, path in calls)


def test_lost_create_response_is_reconciled_without_duplicate_post(tmp_path):
    work, calls, attempts, remote = service(tmp_path)
    original = work.request
    def lost_response(method, path, payload=None):
        result = original(method, path, payload)
        if method == 'POST':
            raise OSError('synthetic response lost after acceptance')
        return result
    work.request = lost_response
    assert work.send('target', SESSION, TEXT, KEY)['outcome'] == 'uncertain'
    assert work.send('target', SESSION, TEXT, KEY)['outcome'] == 'accepted'
    assert sum(path == '/api/instructions' for _, path in calls) == 1


def test_api_denial_and_uncertainty_never_turn_into_delivery(tmp_path):
    work, calls, attempts, remote = service(tmp_path)
    request = httpx.Request('GET', 'https://fixture.example.invalid/api/registered-sessions')
    response = httpx.Response(403, request=request)
    def denied(method, path, payload=None):
        raise httpx.HTTPStatusError('synthetic denied', request=request, response=response)
    work.request = denied
    assert work.target('target')['outcome'] == 'denied'
    assert work.send('target', SESSION, TEXT, KEY)['outcome'] == 'denied'
    assert all(method != 'POST' for method, _ in calls)


def test_mcp_handoff_is_opt_in_and_uses_exact_queue(tmp_path, monkeypatch):
    work, calls, attempts, remote = service(tmp_path)
    monkeypatch.setenv('WB_MCP_REGISTRATION_STATE', str(work.registration_state))
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): return None
        def json(self): return self.value
    class API:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def request(self, method, path, json=None): return Response(work.request(method, path, json))
    server = build_server(api_factory=lambda **kwargs: API(), manifest_path=lambda: None,
                          context_loader=lambda path: work.contexts,
                          handoff_verifier=lambda identity: identity == SESSION)
    async def invoke(name, args):
        async with Client(server) as connected:
            return (await connected.call_tool(name, args)).structured_content
    assert asyncio.run(invoke('agent_handoff_preview', {'context_id': 'target'}))['outcome'] == 'ready'
    args = {'context_id': 'target', 'expected_session_id': SESSION,
            'text': TEXT, 'idempotency_key': KEY}
    assert asyncio.run(invoke('agent_handoff_send', args))['outcome'] == 'denied'
    assert all(method != 'POST' for method, _ in calls)
    monkeypatch.setenv('WB_MCP_ALLOW_HANDOFF', '1')
    assert asyncio.run(invoke('agent_handoff_send', args))['outcome'] == 'accepted'
    assert sum(path == '/api/instructions' for _, path in calls) == 1
