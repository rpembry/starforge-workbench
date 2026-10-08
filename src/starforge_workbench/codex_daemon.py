"""Narrow local Codex app-server client for opt-in shared-daemon threads."""
import json
import os
from pathlib import Path
import re
import stat

from websockets.sync.client import unix_connect


THREAD_ID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')


def _request(connection, number, method, params):
    connection.send(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method,
                                'params': params}, ensure_ascii=False))
    while True:
        response = json.loads(connection.recv(timeout=8))
        if response.get('id') != number:
            continue
        if 'error' in response or 'result' not in response:
            raise ValueError('Codex daemon did not confirm '+method)
        return response['result']


def _connect():
    root = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    socket = root/'app-server-control/app-server-control.sock'
    link = socket.lstat()
    target = socket.resolve(strict=True)
    info = target.stat()
    parent = target.parent.stat()
    if (link.st_uid != os.getuid() or
            not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or
            parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError('Codex daemon socket is unavailable or unsafe')
    return unix_connect(str(socket), uri='ws://localhost/', open_timeout=3,
                        close_timeout=1)


def _initialize(connection, *, experimental=False):
    params = {'clientInfo': {'name': 'starforge-workbench', 'version': '0.2.0'}}
    if experimental:
        params['capabilities'] = {'experimentalApi': True}
    _request(connection, 1, 'initialize', params)
    connection.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}}))


def create_thread(directory: Path, title: str, instructions: str | None, sandbox: str,
                  additional_directories: list[Path] | None = None):
    """Create one named thread; caller journals uncertainty before this call."""
    with _connect() as connection:
        _initialize(connection, experimental=bool(additional_directories))
        params = {'cwd': str(directory), 'approvalPolicy': 'on-request',
                  'sandbox': sandbox, 'serviceName': 'starforge-workbench'}
        if additional_directories:
            params['runtimeWorkspaceRoots'] = [str(directory),
                                                *(str(path) for path in additional_directories)]
        if instructions is not None:
            params['developerInstructions'] = instructions
        thread = _request(connection, 2, 'thread/start', params)['thread']
        identity = thread['id']
        if not THREAD_ID.fullmatch(identity):
            raise ValueError('Codex daemon returned an invalid thread ID')
        _request(connection, 3, 'thread/name/set', {'threadId': identity, 'name': title})
        return identity


def thread_status(identity: str):
    if not THREAD_ID.fullmatch(identity):
        raise ValueError('Invalid Codex thread ID')
    with _connect() as connection:
        _initialize(connection)
        thread = _request(connection, 2, 'thread/read',
                          {'threadId': identity, 'includeTurns': False})['thread']
        if thread.get('id') != identity:
            raise ValueError('Codex daemon returned a different thread')
        return thread.get('status', {}).get('type', 'unknown')


def start_turn(identity: str, prompt: str):
    if not THREAD_ID.fullmatch(identity) or not prompt.strip():
        raise ValueError('Invalid Codex handoff')
    with _connect() as connection:
        _initialize(connection)
        result = _request(connection, 2, 'turn/start',
                          {'threadId': identity, 'input': [{'type': 'text', 'text': prompt}]})
        turn = result['turn']
        if not isinstance(turn.get('id'), str) or not turn['id']:
            raise ValueError('Codex daemon did not confirm a turn ID')
        return turn['id']
