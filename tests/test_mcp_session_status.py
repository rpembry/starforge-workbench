import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from workbench.auth import Auth
from workbench.main import create_app
from workbench.mcp_server import (_exact_id, _page, _safe_instruction, _safe_session,
                                  build_server, SESSION_ID)
from workbench.repository import SQLiteRepository


SESSION = 'registered_session_0001'
INSTRUCTION = 'instruction_00000001'


class Response:
    def __init__(self, value): self.value = value
    def raise_for_status(self): return None
    def json(self): return self.value


class API:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.session = {
            'id': SESSION, 'display_name': 'private-machine-name', 'host': 'private-machine-name',
            'collector_source': 'private-collector', 'owner': 'private-owner',
            'provider': 'opencode', 'visibility': 'stale', 'evidence_state': 'present',
            'reason': 'process_observed', 'observed_at': '2026-01-01T00:00:00+00:00',
            'heartbeat_at': '2026-01-01T00:00:00+00:00', 'last_activity_at': None,
            'summary': 'PRIVATE_PROVIDER_CONTENT'}
        self.instruction = {
            'id': INSTRUCTION, 'registered_session_id': SESSION, 'state': 'queued',
            'reason_code': 'operator_created', 'created_at': '2026-01-01T00:00:00+00:00',
            'updated_at': '2026-01-01T00:00:00+00:00', 'expires_at': '2026-01-01T00:15:00+00:00',
            'received_at': None, 'responded_at': None, 'terminal_at': None,
            'text': 'PRIVATE_INSTRUCTION_BODY', 'idempotency_key': 'private-key-0000001',
            'lease_token_hash': 'PRIVATE_LEASE', 'claim_owner': 'PRIVATE_HOST',
            'history': [{'state': 'queued', 'reason_code': 'operator_created',
                         'occurred_at': '2026-01-01T00:00:00+00:00', 'actor': 'PRIVATE_OPERATOR',
                         'excerpt': 'PRIVATE_EXCERPT'}]}

    def __enter__(self): return self
    def __exit__(self, *args): return None
    def request(self, method, path, json=None):
        if self.fail: raise OSError('synthetic unavailable backend')
        self.calls.append((method, path, json))
        if path.startswith('/api/registered-sessions?'): return Response({'items': [self.session]})
        if path == '/api/registered-sessions/' + SESSION: return Response(self.session)
        if path.startswith('/api/instructions?'): return Response({'items': [self.instruction]})
        if path == '/api/instructions/' + INSTRUCTION: return Response(self.instruction)
        raise AssertionError(path)


def invoke_raw(server, name, arguments=None):
    async def run():
        async with Client(server) as connected:
            return await connected.call_tool(name, arguments or {})
    return asyncio.run(run())


def invoke(server, name, arguments=None):
    return invoke_raw(server, name, arguments).structured_content


def test_bounded_read_only_facade_uses_operator_api_and_omits_private_fields():
    api = API()
    server = build_server(api_factory=lambda **kwargs: api)
    listed = invoke(server, 'registered_session_list', {'limit': 2})
    detail = invoke(server, 'registered_session_detail', {'session_id': SESSION})
    timeline = invoke(server, 'registered_session_instructions', {'session_id': SESSION, 'limit': 3})
    status = invoke(server, 'registered_instruction_status', {
        'session_id': SESSION, 'instruction_id': INSTRUCTION})
    assert listed['items'][0] == detail['session'] == _safe_session(api.session)
    assert detail['session']['visibility'] == 'stale'
    assert detail['session']['send_eligible'] is False
    assert timeline['items'][0] == _safe_instruction(api.instruction)
    assert status['instruction'] == _safe_instruction(api.instruction, history=True)
    assert all(result['api_status'] == 'available' and result['checked_at'] for result in
               (listed, detail, timeline, status))
    output = repr((listed, detail, timeline, status))
    assert all(private not in output for private in ('private-machine-name', 'private-collector',
        'PRIVATE_PROVIDER_CONTENT', 'PRIVATE_INSTRUCTION_BODY', 'PRIVATE_LEASE',
        'PRIVATE_HOST', 'PRIVATE_OPERATOR', 'PRIVATE_EXCERPT', 'private-key-0000001'))
    assert api.calls == [
        ('GET', '/api/registered-sessions?limit=2&offset=0', None),
        ('GET', '/api/registered-sessions/' + SESSION, None),
        ('GET', '/api/instructions?limit=3&offset=0&registered_session_id=' + SESSION, None),
        ('GET', '/api/instructions/' + INSTRUCTION, None)]


def test_facade_rejects_invalid_identity_bounds_and_unrelated_rows():
    for identity in ('../../api/secrets', 'short', 'registered_session_0001?limit=500'):
        with pytest.raises(ValueError): _exact_id(identity, SESSION_ID, 'session_id')
    for limit, offset in ((0, 0), (51, 0), (1, -1), (1, 10001)):
        with pytest.raises(ValueError): _page(limit, offset)
    api = API()
    invalid_server = build_server(api_factory=lambda **kwargs: api)
    assert invoke_raw(invalid_server, 'registered_session_detail',
                      {'session_id': '../../api/secrets'}).is_error
    assert api.calls == []
    api.instruction['registered_session_id'] = 'registered_session_0002'
    server = build_server(api_factory=lambda **kwargs: api)
    assert invoke_raw(server, 'registered_instruction_status', {
        'session_id': SESSION, 'instruction_id': INSTRUCTION}).is_error
    assert invoke_raw(server, 'registered_session_instructions', {'session_id': SESSION}).is_error


def test_facade_truncates_oversized_api_pages_and_history():
    api = API()
    original_request = api.request
    def oversized(method, path, json=None):
        response = original_request(method, path, json)
        if path.startswith('/api/registered-sessions?'):
            return Response({'items': [api.session] * 60})
        if path.startswith('/api/instructions?'):
            return Response({'items': [api.instruction] * 60})
        if path == '/api/instructions/' + INSTRUCTION:
            return Response({**api.instruction, 'history': api.instruction['history'] * 60})
        return response
    api.request = oversized
    server = build_server(api_factory=lambda **kwargs: api)
    assert len(invoke(server, 'registered_session_list', {'limit': 2})['items']) == 2
    assert len(invoke(server, 'registered_session_instructions', {'session_id': SESSION, 'limit': 3})['items']) == 3
    assert len(invoke(server, 'registered_instruction_status', {
        'session_id': SESSION, 'instruction_id': INSTRUCTION})['instruction']['history']) == 50


def test_unavailable_backend_is_not_reported_as_offline_session():
    api = API()
    api.fail = True
    server = build_server(api_factory=lambda **kwargs: api)
    result = invoke_raw(server, 'registered_session_detail', {'session_id': SESSION})
    assert result.is_error
    assert 'offline' not in repr(result).lower()


def test_read_only_facade_matches_existing_operator_api(tmp_path):
    repo = SQLiteRepository(tmp_path / 'state' / 'workbench.sqlite')
    with TestClient(create_app(repo, Auth({'operator': 'o' * 40, 'collector': 'c' * 40}))) as api:
        api.headers['Authorization'] = 'Bearer ' + 'c' * 40
        assert api.post('/api/collectors/heartbeat', json={
            'source': 'synthetic-collector', 'instance_id': 'synthetic-instance', 'scope': 'synthetic',
            'status': 'ok', 'reason': 'scan_complete'}).status_code == 200
        assert api.post('/api/registered-sessions', json={
            'id': SESSION, 'collector_source': 'synthetic-collector', 'host': 'private-host',
            'display_name': 'Synthetic agent', 'provider': 'opencode', 'evidence_state': 'present',
            'reason': 'process_observed', 'summary': 'PRIVATE_TRANSCRIPT', 'observation_sequence': 1,
            'observed_at': datetime.now(timezone.utc).isoformat()}).status_code == 201
        api.headers['Authorization'] = 'Bearer ' + 'o' * 40
        created = api.post('/api/instructions', json={
            'idempotency_key': 'synthetic-mcp-key-0001', 'registered_session_id': SESSION,
            'text': 'PRIVATE_INSTRUCTION', 'expiry_minutes': 15},
            headers={'Origin': 'http://testserver'})
        assert created.status_code == 201

        class Adapter:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def request(self, method, path, json=None):
                return api.request(method, path, json=json)

        server = build_server(api_factory=lambda **kwargs: Adapter())
        direct_session = api.get('/api/registered-sessions/' + SESSION).json()
        direct_instruction = api.get('/api/instructions/' + created.json()['id']).json()
        assert invoke(server, 'registered_session_detail', {'session_id': SESSION})['session'] == _safe_session(direct_session)
        status = invoke(server, 'registered_instruction_status', {
            'session_id': SESSION, 'instruction_id': created.json()['id']})['instruction']
        assert status == _safe_instruction(direct_instruction, history=True)
        assert 'PRIVATE_INSTRUCTION' not in repr(status)
        assert 'private-host' not in repr(invoke(server, 'registered_session_detail', {'session_id': SESSION}))
