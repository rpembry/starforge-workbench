"""Synthetic contract checks for the narrow local app-server adapter."""
import json
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from starforge_workbench import codex_daemon


class Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, timeout=None):
        return json.dumps(next(self.responses))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_create_thread_seeds_role_and_names_exact_id(tmp_path):
    path = tmp_path/'app-server-control/app-server-control.sock'
    path.parent.mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    identity = '11111111-2222-3333-4444-555555555555'
    replies = [
        {'id': 1, 'result': {}},
        {'method': 'thread/started', 'params': {}},
        {'id': 2, 'result': {'thread': {'id': identity}}},
        {'id': 3, 'result': {}},
    ]
    connection = Connection(replies)
    with patch.dict('os.environ', {'CODEX_HOME': str(tmp_path)}), \
            patch.object(codex_daemon, 'unix_connect', return_value=connection):
        assert codex_daemon.create_thread(Path('/tmp/synthetic'), 'Synthetic Support',
                                          'Support role', 'workspace-write') == identity
    listener.close()
    start = next(message for message in connection.sent if message.get('method') == 'thread/start')
    assert start['params']['developerInstructions'] == 'Support role'
    assert start['params']['cwd'] == '/tmp/synthetic'
    assert start['params']['approvalPolicy'] == 'on-request'
    assert 'runtimeWorkspaceRoots' not in start['params']
    assert connection.sent[-1]['params'] == {'threadId': identity, 'name': 'Synthetic Support'}


def test_create_thread_preserves_additional_workspace_roots(tmp_path):
    path = tmp_path/'app-server-control/app-server-control.sock'
    path.parent.mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    identity = '11111111-2222-3333-4444-555555555555'
    connection = Connection([
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'thread': {'id': identity}}},
        {'id': 3, 'result': {}},
    ])
    with patch.dict('os.environ', {'CODEX_HOME': str(tmp_path)}), \
            patch.object(codex_daemon, 'unix_connect', return_value=connection):
        codex_daemon.create_thread(Path('/tmp/example.invalid/main'), 'Synthetic', None,
                                   'workspace-write', [Path('/tmp/example.invalid/shared')])
    listener.close()
    start = next(message for message in connection.sent if message.get('method') == 'thread/start')
    initialize = next(message for message in connection.sent if message.get('method') == 'initialize')
    assert initialize['params']['capabilities'] == {'experimentalApi': True}
    assert start['params']['runtimeWorkspaceRoots'] == [
        '/tmp/example.invalid/main', '/tmp/example.invalid/shared']


def test_create_thread_rejects_unconfirmed_name(tmp_path):
    path = tmp_path/'app-server-control/app-server-control.sock'
    path.parent.mkdir(mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    identity = '11111111-2222-3333-4444-555555555555'
    connection = Connection([
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'thread': {'id': identity}}},
        {'id': 3, 'error': {'message': 'synthetic failure'}},
    ])
    with patch.dict('os.environ', {'CODEX_HOME': str(tmp_path)}), \
            patch.object(codex_daemon, 'unix_connect', return_value=connection):
        with pytest.raises(ValueError, match='did not confirm thread/name/set'):
            codex_daemon.create_thread(Path('/tmp/synthetic'), 'Synthetic', None, 'read-only')
    listener.close()
