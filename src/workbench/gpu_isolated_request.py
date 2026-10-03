"""Opt-in, single synthetic Ollama request inside an exact user-systemd unit.

The worker must itself be the supervised UDS peer. Its provider child inherits
the same cgroup. Nothing in this module changes the normal Ollama service,
miner unit, SSH tunnel, or systemd configuration at import time.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
from urllib import request


class RequestUnavailable(RuntimeError):
    pass


class SocketLeaseClient:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('A private absolute socket path is required')

    def _call(self, payload: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(str(self.path))
                connection.sendall((json.dumps(payload, separators=(',', ':')) + '\n').encode())
                data = b''
                while not data.endswith(b'\n') and len(data) <= 4096:
                    part = connection.recv(4097 - len(data))
                    if not part:
                        break
                    data += part
            answer = json.loads(data)
            if (not data.endswith(b'\n') or len(data) > 4096 or
                    not isinstance(answer, dict) or answer.get('ok') is not True or
                    not isinstance(answer.get('result'), dict)):
                raise RequestUnavailable('Lease adapter rejected the request')
            return answer['result']
        except (OSError, ValueError, TypeError) as exc:
            raise RequestUnavailable('Lease adapter is unavailable') from exc

    def acquire(self, key: str, ttl: float) -> dict:
        return self._call({'op': 'acquire', 'key': key, 'ttl': ttl})

    def status(self, token: str) -> dict:
        return self._call({'op': 'status', 'token': token})

    def finish(self, token: str) -> dict:
        return self._call({'op': 'finish', 'token': token})


class OllamaServe:
    """One child server on a separate loopback port; no model installation."""

    def __init__(self, model: str, port: int, *, models_path: Path | None = None):
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', model):
            raise ValueError('A bounded installed model name is required')
        if not (1024 <= port <= 65535) or port == 11434:
            raise ValueError('Use a non-default high loopback port')
        self.model = model
        self.port = port
        self.models_path = models_path
        self.base = f'http://127.0.0.1:{port}'
        self.child = None
        self.lock = threading.RLock()
        self.opener = request.build_opener(request.ProxyHandler({}))

    def _json(self, path: str, payload: dict | None = None, *, timeout: float = 3) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        req = request.Request(self.base + path, data=data,
                              headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=timeout) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise RequestUnavailable('Ollama response exceeded limit')
        result = json.loads(body)
        if not isinstance(result, dict):
            raise RequestUnavailable('Invalid Ollama response')
        return result

    def start(self) -> None:
        # Refuse to attach to an existing service on this port.
        try:
            with socket.create_connection(('127.0.0.1', self.port), timeout=0.2):
                raise RequestUnavailable('Private Ollama port is already in use')
        except ConnectionRefusedError:
            pass
        env = os.environ.copy()
        env['OLLAMA_HOST'] = f'127.0.0.1:{self.port}'
        if self.models_path is not None:
            if not self.models_path.is_absolute():
                raise ValueError('Model cache path must be absolute')
            env['OLLAMA_MODELS'] = str(self.models_path)
        with self.lock:
            self.child = subprocess.Popen(('ollama', 'serve'), env=env,
                                          stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise RequestUnavailable('Private Ollama server exited')
            try:
                self._json('/api/version', timeout=0.5)
                return
            except (OSError, ValueError, RequestUnavailable):
                time.sleep(0.1)
        raise RequestUnavailable('Private Ollama server did not become ready')

    def generate(self) -> None:
        tags = self._json('/api/tags')
        models = tags.get('models')
        if not isinstance(models, list) or self.model not in {
                row.get('name') for row in models if isinstance(row, dict)}:
            raise RequestUnavailable('Requested model is not in the existing cache')
        result = self._json('/api/generate', {
            'model': self.model, 'prompt': 'Reply with exactly READY.',
            'stream': False, 'keep_alive': 0, 'options': {'num_predict': 8},
        }, timeout=120)
        if result.get('done') is not True:
            raise RequestUnavailable('Synthetic generation did not finish')

    def stop(self) -> bool:
        with self.lock:
            child = self.child
            if child is None or child.poll() is not None:
                return True
            child.terminate()
        try:
            child.wait(timeout=15)
            return True
        except subprocess.TimeoutExpired:
            # No guessed group kill. The cgroup and durable lease must remain
            # until the exact supervisor proves that all children are gone.
            return False


class SingleSyntheticRequest:
    TTL = 240

    def __init__(self, leases, provider, *, clock=time.monotonic, sleep=time.sleep,
                 grant_timeout: float = 30, watch_interval: float = 0.5):
        if not (0 < grant_timeout <= 60 and 0 < watch_interval <= 5):
            raise ValueError('Request timing bounds are invalid')
        self.leases = leases
        self.provider = provider
        self.clock = clock
        self.sleep = sleep
        self.grant_timeout = grant_timeout
        self.watch_interval = watch_interval

    def _grant_is_current(self, token: str) -> bool:
        """Treat mismatched, stale, expired, or malformed status as unsafe."""
        state = self.leases.status(token)
        if not isinstance(state, dict):
            raise RequestUnavailable('Lease status is incomplete or mismatched')
        expiry = state.get('expires_at_monotonic')
        if (state.get('token') != token or type(state.get('granted')) is not bool or
                type(state.get('stale')) is not bool or
                type(expiry) not in (int, float) or not math.isfinite(expiry)):
            raise RequestUnavailable('Lease status is incomplete or mismatched')
        if state['stale'] or self.clock() >= expiry:
            raise RequestUnavailable('Lease became stale or expired')
        return state['granted']

    def run(self, key: str) -> None:
        token = self.leases.acquire(key, self.TTL).get('token')
        if not isinstance(token, str) or not token:
            raise RequestUnavailable('Lease acquisition is incomplete')
        failure = []
        finished = threading.Event()
        watcher = None
        try:
            deadline = self.clock() + self.grant_timeout
            while True:
                if self._grant_is_current(token):
                    break
                if self.clock() >= deadline:
                    raise RequestUnavailable('Miner exit was not confirmed in time')
                self.sleep(0.25)

            def guard():
                while not finished.wait(self.watch_interval):
                    try:
                        if self._grant_is_current(token):
                            continue
                    except RequestUnavailable:
                        pass
                    failure.append('Lease grant was lost or became unknown')
                    self.provider.stop()
                    return

            watcher = threading.Thread(target=guard, daemon=True)
            watcher.start()
            self.provider.start()
            if failure or not self._grant_is_current(token):
                raise RequestUnavailable('Lease grant was lost before inference')
            self.provider.generate()
            if failure or not self._grant_is_current(token):
                raise RequestUnavailable('Lease grant was lost during inference')
        finally:
            finished.set()
            if watcher is not None:
                watcher.join(timeout=1)
            stopped = self.provider.stop()
            # Finish is a durable closing request, not an immediate release.
            # If it fails, the TTL and authoritative scope-end check retain
            # the hold. If stop fails, live scope members retain the hold.
            self.leases.finish(token)
            if not stopped:
                raise RequestUnavailable('Private Ollama scope did not stop')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='One isolated synthetic GPU request')
    parser.add_argument('--socket', required=True, type=Path)
    parser.add_argument('--model', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--models-path', type=Path)
    args = parser.parse_args(argv)
    job = SingleSyntheticRequest(SocketLeaseClient(args.socket),
                                 OllamaServe(args.model, args.port,
                                             models_path=args.models_path))
    try:
        job.run(secrets.token_urlsafe(18))
    except (RequestUnavailable, OSError, ValueError) as exc:
        parser.exit(1, f'synthetic request unavailable: {exc}\n')
    print('Synthetic request completed; scope-end reconciliation is still required.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
