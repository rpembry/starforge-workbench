"""Local machine clients may carry only the authority they need."""
import json

import pytest
from fastapi.testclient import TestClient

from workbench.auth import Auth
from workbench.client import client
from workbench.main import create_app
from workbench.repository import SQLiteRepository

OPERATOR = 'o' * 40
COLLECTOR = 'c' * 40
URL = 'http://127.0.0.1:8027'


def credential_file(tmp_path, data):
    path = tmp_path / 'client.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_collector_only_client_authenticates_without_operator_token(tmp_path):
    path = credential_file(tmp_path, {'url': URL, 'role': 'collector', 'token': COLLECTOR})
    with client(credentials_file=path, role='collector') as collector:
        authorization = collector.headers['Authorization']
    assert authorization == 'Bearer ' + COLLECTOR
    assert OPERATOR not in path.read_text()
    with TestClient(create_app(SQLiteRepository(tmp_path / 'state' / 'db.sqlite'),
                               Auth({'operator': OPERATOR, 'collector': COLLECTOR}))) as api:
        headers = {'Authorization': authorization}
        assert api.get('/api/attention', headers=headers).status_code == 403
        assert api.post('/api/collectors/heartbeat', headers=headers, json={
            'source': 'fixture', 'instance_id': 'one', 'scope': 'fixture',
            'status': 'ok', 'reason': 'scan_complete'}).status_code == 200


def test_role_bound_file_rejects_other_role_origin_and_unsafe_shape(tmp_path):
    path = credential_file(tmp_path, {'url': URL, 'role': 'collector', 'token': COLLECTOR})
    with pytest.raises(ValueError, match='not authorized'):
        client(credentials_file=path, role='operator')
    with pytest.raises(ValueError, match='configured URL'):
        client('https://example.invalid', path, 'collector')
    path.write_text(json.dumps({'url': URL, 'role': 'collector', 'token': COLLECTOR,
                                'operator': OPERATOR}))
    with pytest.raises(RuntimeError, match='separate operator and collector'):
        client(credentials_file=path, role='collector')


def test_legacy_server_file_still_works_for_both_roles(tmp_path):
    path = credential_file(tmp_path, {'operator': OPERATOR, 'collector': COLLECTOR})
    with client(credentials_file=path, role='operator') as operator:
        assert operator.headers['Authorization'] == 'Bearer ' + OPERATOR
    with client(credentials_file=path, role='collector') as collector:
        assert collector.headers['Authorization'] == 'Bearer ' + COLLECTOR
