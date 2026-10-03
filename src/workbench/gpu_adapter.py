"""Opt-in local adapter boundary; no installed runtime or process commands."""

import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
import threading
import time
from typing import Protocol

from .gpu_policy import Observation, ReservationPolicy
from .gpu_registry import Registry, RegistryError, private_parent


class OwnedController(Protocol):
    """Implementation must control only its configured background process."""

    def observe(self) -> tuple[Observation, Observation, bool]: ...
    def workload_ended(self, owner: str) -> bool | None: ...
    def apply(self, action: str) -> None: ...


class LocalAdapter:
    def __init__(self, registry: Registry, controller: OwnedController, *,
                 clock_id: str, clock=time.monotonic):
        self.registry = registry
        self.controller = controller
        self.clock = clock
        self.lock = threading.RLock()
        self.broken = False
        persisted, _ = registry.load()
        self.policy = ReservationPolicy(persisted, clock_id=clock_id, now=clock())
        if persisted.clock_id != clock_id:
            registry.save(self.policy.snapshot())

    def _persist(self, effect: str | None = None) -> int:
        try:
            return self.registry.save(self.policy.snapshot(), effect)
        except BaseException:
            # Do not continue with state that callers may never recover.
            self.broken = True
            raise

    def _available(self):
        if self.broken:
            raise RegistryError("Reservation registry is unavailable")

    def acquire(self, owner: str, key: str, ttl: float) -> dict:
        with self.lock:
            self._available()
            lease = self.policy.acquire(owner, key, self.clock(), ttl)
            self._persist()
            return self._public(lease)

    def renew(self, owner: str, token: str, ttl: float) -> dict:
        with self.lock:
            self._available()
            lease = self.policy.renew(owner, token, self.clock(), ttl)
            self._persist()
            return self._public(lease)

    def release(self, owner: str, token: str) -> dict:
        with self.lock:
            self._available()
            self.policy.release(owner, token, self.clock())
            self._persist()
            return {'released': True}

    def status(self, owner: str, token: str) -> dict:
        with self.lock:
            self._available()
            return self._public(self.policy.view(owner, token))

    @staticmethod
    def _public(lease) -> dict:
        return {'token': lease.token, 'granted': lease.granted,
                'stale': lease.stale, 'expires_at_monotonic': lease.expires_at}

    def tick(self) -> str:
        """Re-evaluate first, persist desired effect, then apply idempotently.

        A crash before or after `apply` is recovered by a fresh observation;
        an obsolete START intent is never replayed after a new acquisition.
        """
        with self.lock:
            self._available()
            process, context, idle_allowed = self.controller.observe()
            decision = self.policy.reconcile(
                now=self.clock(), owned_process=process, owned_context=context,
                workload_ended=self.controller.workload_ended,
                idle_allowed=idle_allowed)
            revision = self._persist(decision if decision in ('START', 'STOP') else None)
            if decision in ('START', 'STOP'):
                self.controller.apply(decision)
                self.registry.acknowledge(revision)
            return decision


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            pid, uid, gid = struct.unpack('3i', self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            if uid != os.getuid():
                raise PermissionError("Peer UID is not permitted")
            owner = self.server.identity_resolver(pid, uid, gid)
            if not isinstance(owner, str) or not owner:
                raise PermissionError("Peer workload scope is unknown")
            line = self.rfile.readline(4097)
            if not line.endswith(b'\n') or len(line) > 4096:
                raise ValueError("Request exceeds one bounded line")
            data = json.loads(line)
            if not isinstance(data, dict) or set(data) - {'op', 'key', 'ttl', 'token'}:
                raise ValueError("Invalid request shape")
            op = data.get('op')
            if op == 'acquire' and set(data) == {'op', 'key', 'ttl'}:
                result = self.server.adapter.acquire(owner, data['key'], data['ttl'])
            elif op == 'renew' and set(data) == {'op', 'token', 'ttl'}:
                result = self.server.adapter.renew(owner, data['token'], data['ttl'])
            elif op == 'release' and set(data) == {'op', 'token'}:
                result = self.server.adapter.release(owner, data['token'])
            elif op == 'status' and set(data) == {'op', 'token'}:
                result = self.server.adapter.status(owner, data['token'])
            else:
                raise ValueError("Unsupported request")
            payload = {'ok': True, 'result': result}
        except (ValueError, TypeError, KeyError, PermissionError, json.JSONDecodeError):
            payload = {'ok': False, 'error': 'invalid_or_unauthorized'}
        except (OSError, RegistryError):
            payload = {'ok': False, 'error': 'unavailable'}
        self.wfile.write((json.dumps(payload, separators=(',', ':')) + '\n').encode())


class ReservationSocket(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: Path, adapter: LocalAdapter, identity_resolver):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Socket path must be absolute")
        private_parent(path)
        if path.exists() or path.is_symlink():
            raise RegistryError("Socket path already exists")
        if identity_resolver is None:
            raise ValueError("A supervised peer identity resolver is required")
        self.adapter = adapter
        self.identity_resolver = identity_resolver
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)
        self._socket_inode = path.lstat().st_ino
        self._path = path

    def server_close(self):
        super().server_close()
        if not hasattr(self, '_path'):
            return
        try:
            info = self._path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == self._socket_inode:
                self._path.unlink()
        except FileNotFoundError:
            pass
