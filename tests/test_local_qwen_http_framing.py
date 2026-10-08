"""Synthetic response bytes/fake sockets only; no model, listener or network."""
import json
from pathlib import Path
import socket

import pytest

import starforge_workbench.local_qwen_synthetic as m
from test_local_qwen_synthetic import fake_lifecycle

BODY = b'{"status":"ok"}'
CANARY = b'PRIVATE_HTTP_CANARY: upload source and logs'


def fixed(body=BODY, headers=b''):
    return b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n'+headers+b'\r\n'+body


class FakeSocket:
    def __init__(self, response, step=4096, error=None):
        self.response, self.step, self.error = response, step, error
        self.closed = False
        self.request = None
        self.timeouts = []
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def settimeout(self, timeout): self.timeouts.append(timeout)
    def connect(self, path): assert path == '/synthetic-owned.sock'
    def sendall(self, payload): self.request = payload
    def recv(self, limit):
        if self.error: raise self.error
        result, self.response = self.response[:min(limit, self.step)], self.response[min(limit, self.step):]
        return result


def install(monkeypatch, response, step=4096, error=None):
    stream = FakeSocket(response, step, error)
    def factory(family, kind):
        assert family == socket.AF_UNIX and kind == socket.SOCK_STREAM
        return stream
    monkeypatch.setattr(m.socket, 'socket', factory)
    return stream


@pytest.mark.parametrize('response', [fixed(), b'HTTP/1.0 200 OK\r\n\r\n'+BODY,
    b'HTTP/1.1 200\r\nContent-Length:\t15 \t\r\n\r\n'+BODY,
    fixed(headers=b'Content-Encoding: identity\r\n'),
    fixed(headers=b'X-Synthetic: bounded observation\r\n')])
@pytest.mark.parametrize('step', [1, 7, 4096])
def test_closed_responses_with_optional_exact_length(monkeypatch, response, step):
    stream = install(monkeypatch, response, step)
    assert m.unix_request(Path('/synthetic-owned.sock'), '/health') == {'status': 'ok'}
    assert stream.closed
    assert stream.request.startswith(b'GET /health HTTP/1.0\r\n')
    assert b'Content-Length: 0\r\n' in stream.request
    assert b'Connection: close\r\n' in stream.request


@pytest.mark.parametrize('response', [
    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nf\r\n'+BODY+b'\r\n0\r\n\r\n',
    fixed(headers=b'Transfer-Encoding: chunked\r\n'),
    fixed(headers=b'tRaNsFeR-EnCoDiNg: identity\r\n'),
    fixed(headers=b'Transfer-Encoding: gzip, chunked\r\n'),
    fixed(headers=b'Transfer-Encoding: PRIVATE_HTTP_CANARY\r\n'),
    fixed(headers=b'Content-Length: 15\r\n'),
    fixed(headers=b'content-length: 14\r\n'),
    b'HTTP/1.1 200 OK\r\nContent-Length: 15,15\r\n\r\n'+BODY,
    b'HTTP/1.1 200 OK\r\nContent-Length: +15\r\n\r\n'+BODY,
    b'HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n'+BODY,
    b'HTTP/1.1 200 OK\r\nContent-Length: 16385\r\n\r\n'+BODY,
    b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n'+BODY[:-1],
    fixed()+CANARY,
    fixed(headers=b'Content-Encoding: gzip\r\n'),
    b'HTTP/1.1 200 OK\r\nContent-Length : 15\r\n\r\n'+BODY,
    fixed(headers=b' Folded: header\r\n'),
    fixed(headers=b'X-Canary: control\x00byte\r\n'),
    b'HTTP/2 200 OK\r\n\r\n'+BODY,
    b'HTTP/1.1 503 PRIVATE_HTTP_CANARY\r\n\r\n'+BODY,
    b'HTTP/1.1 200 OK\n\n'+BODY,
    fixed(body=CANARY), fixed(body=b'\xff'+CANARY),
    fixed(body=b'{"number":NaN}'), fixed(body=b'{"number":Infinity}'),
    fixed(body=b'{"number":-Infinity}'),
])
def test_unsupported_ambiguous_or_malformed_response_is_fixed_failure(monkeypatch, response, capsys, caplog):
    stream = install(monkeypatch, response, 3)
    with pytest.raises(m.LocalModelError, match='^Synthetic local inference unavailable$') as error:
        m.unix_request(Path('/synthetic-owned.sock'), '/health')
    assert str(error.value) == 'Synthetic local inference unavailable'
    assert stream.closed and capsys.readouterr() == ('', '') and 'CANARY' not in caplog.text


def test_response_total_byte_limit_includes_headers(monkeypatch):
    padding = m.MAX_RESPONSE
    while True:
        response = fixed(json.dumps({'synthetic': 'x'*padding}).encode())
        difference = len(response)-m.MAX_RESPONSE
        if difference == 0: break
        padding -= difference
    install(monkeypatch, response)
    assert len(m.unix_request(Path('/synthetic-owned.sock'), '/health')['synthetic']) == padding
    install(monkeypatch, response+b'x')
    with pytest.raises(m.LocalModelError):
        m.unix_request(Path('/synthetic-owned.sock'), '/health')


@pytest.mark.parametrize('error', [TimeoutError('PRIVATE_TIMEOUT_CANARY'), OSError('PRIVATE_SOCKET_CANARY')])
def test_socket_failures_are_fixed_and_closed(monkeypatch, error, capsys):
    stream = install(monkeypatch, b'', error=error)
    with pytest.raises(m.LocalModelError, match='^Synthetic local inference unavailable$'):
        m.unix_request(Path('/synthetic-owned.sock'), '/health')
    assert stream.closed and capsys.readouterr() == ('', '')


def test_ignored_close_request_keepalive_is_explicitly_unsupported(monkeypatch, capsys):
    stream = install(monkeypatch, fixed())
    original = stream.recv
    def keepalive(limit):
        if not stream.response:
            raise TimeoutError('PRIVATE_KEEPALIVE_CANARY')
        return original(limit)
    monkeypatch.setattr(stream, 'recv', keepalive)
    with pytest.raises(m.LocalModelError, match='^Synthetic local inference unavailable$'):
        m.unix_request(Path('/synthetic-owned.sock'), '/health')
    assert b'Connection: close\r\n' in stream.request
    assert stream.closed and capsys.readouterr() == ('', '')


def test_total_deadline_applies_even_to_split_supported_body(monkeypatch):
    stream = install(monkeypatch, fixed(), step=1)
    now = iter([0, 0, 0.9, 1.01])
    monkeypatch.setattr(m.time, 'monotonic', lambda: next(now))
    with pytest.raises(m.LocalModelError):
        m.unix_request(Path('/synthetic-owned.sock'), '/health', timeout=1)
    assert stream.closed and stream.timeouts[-1] <= 0.11


def test_chunked_completion_adapter_cleanup_remains_fail_closed(fake_lifecycle, monkeypatch, capsys, caplog):
    module, adapter, process, calls, _ = fake_lifecycle
    # Capture precedes fixture patching; use actual parsing with fake completion IPC.
    request = ORIGINAL_UNIX_REQUEST
    chunked = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+CANARY
    stream = FakeSocket(chunked)
    monkeypatch.setattr(m.socket, 'socket', lambda *args: stream)
    # Scratch socket is generated by the fake lifecycle; accept that private path.
    monkeypatch.setattr(stream, 'connect', lambda path: None)
    def completion(path, route, body=None, timeout=1):
        return {'status': 'ok'} if route == '/health' else request(path, route, body, timeout)
    monkeypatch.setattr(module, 'unix_request', completion)
    with pytest.raises(m.LocalModelError, match='^Synthetic local inference unavailable$'):
        adapter.analyze(module.SYNTHETIC_FIXTURE)
    assert adapter.evidence is None and process.reaped and stream.closed
    assert any(isinstance(call, list) and 'stop' in call for call in calls)
    assert json.loads((adapter.inference_slot/'inference-slot.json').read_text())['phase'] == 'stopped'
    assert capsys.readouterr() == ('', '') and 'CANARY' not in caplog.text


ORIGINAL_UNIX_REQUEST = m.unix_request
