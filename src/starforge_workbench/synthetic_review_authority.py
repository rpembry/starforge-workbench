"""Offline synthetic human-session fixture, never production authentication.

A trusted test composition supplies synthetic session seeds; there is no login,
credential generation, service activation, signing key, or network transport.
Cookie resolution is separate from approval: only an instance-issued in-memory
capability can approve. Protected authority records are the trusted source of
truth. They must be outside the mutable broker journal and inaccessible to the
model identity. Private modes/checksums do not defend against another process
with the same OS identity, deletion of revocation tombstones, or an administrator
rolling back the whole authority directory. No host isolation claim is made.
"""
from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .diagnostic_release import Approval, DESTINATION, MAX_PAYLOAD, ReleaseError, SYNTHETIC_DESTINATIONS
from .docker_worker import attempt_lock, private_directory

MAX_RECEIPTS = 128
MAX_RECORD = 16384
_BINDING_KEYS = frozenset({'protocol', 'job_id', 'attempt_id', 'operation_id', 'revision',
                           'destination', 'payload', 'sha256', 'expires_at', 'decision'})


@dataclass(frozen=True)
class SyntheticSessionSeed:
    token: str = field(repr=False)
    csrf: str = field(repr=False)
    principal: str
    kind: str
    expires_at: float


@dataclass(frozen=True)
class ResolvedSyntheticSession:
    credential: object = field(repr=False)
    csrf: str = field(repr=False)
    principal: str


def _number(value):
    return type(value) in {int, float} and math.isfinite(value)


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is not None


def _receipt_id(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _binding(value, destinations=frozenset({DESTINATION})):
    """Copy only the exact versioned release contract; instructions stay data."""
    try:
        if type(value) is not dict or value.keys() != _BINDING_KEYS:
            raise ReleaseError()
        if (value['protocol'] != 'diagnostic.release.v1'
                or not _identifier(value['job_id']) or not _identifier(value['attempt_id'])
                or not _receipt_id(value['operation_id']) or type(value['revision']) is not int
                or value['revision'] < 1 or value['destination'] not in destinations
                or type(value['payload']) is not str or len(value['payload']) > 10924
                or type(value['sha256']) is not str or not _number(value['expires_at'])
                or type(value['decision']) is not str or value['decision'] not in {'approve', 'reject', 'defer'}):
            raise ReleaseError()
        payload = base64.b64decode(value['payload'], validate=True)
        if (len(payload) > MAX_PAYLOAD or base64.b64encode(payload).decode('ascii') != value['payload']
                or hashlib.sha256(payload).hexdigest() != value['sha256']):
            raise ReleaseError()
        return json.loads(_canonical(value))
    except Exception:
        raise ReleaseError() from None


def _read(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_RECORD):
            raise ReleaseError()
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            content = stream.read(MAX_RECORD + 1)
        if len(content) > MAX_RECORD:
            raise ReleaseError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ReleaseError()
                result[key] = value
            return result
        value = json.loads(content, object_pairs_hook=unique)
        if type(value) is not dict:
            raise ReleaseError()
        return value
    except Exception:
        raise ReleaseError() from None
    finally:
        if fd is not None:
            os.close(fd)


def _write_once(path, value):
    """A partial crash record fails closed; never replace an issued receipt."""
    content = _canonical(value) + b'\n'
    if len(content) > MAX_RECORD:
        raise ReleaseError()
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        raise ReleaseError() from None
    finally:
        if fd is not None:
            os.close(fd)


class SyntheticHumanAuthority:
    """Protected local fixture authority implementing the broker verifier seam.

    Reopening can verify old receipts but never renews sessions: supplied seeds
    retain their original expiry and old instance capabilities remain invalid.
    Immutable issuance records plus separate revocation tombstones survive
    rollback of a broker journal or issuance record. Whole-authority rollback is
    excluded by the explicit trusted-directory boundary above.
    """
    def __init__(self, authority_path: Path, seeds: tuple[SyntheticSessionSeed, ...], clock=time.time,
                 *, destinations=frozenset({DESTINATION})):
        try:
            self.path = private_directory(authority_path)
            if (type(seeds) is not tuple or len(seeds) > 32 or not callable(clock)
                    or type(destinations) is not frozenset or not destinations
                    or not destinations <= SYNTHETIC_DESTINATIONS):
                raise ReleaseError()
            self.destinations = destinations
            self.clock = clock
            self._sessions = {}
            self._capabilities = []
            for seed in seeds:
                if (type(seed) is not SyntheticSessionSeed or type(seed.token) is not str
                        or not 1 <= len(seed.token) <= 256 or type(seed.csrf) is not str
                        or not 1 <= len(seed.csrf) <= 256 or not _identifier(seed.principal)
                        or seed.kind not in {'synthetic_human', 'service', 'machine'}
                        or not _number(seed.expires_at) or seed.token in self._sessions):
                    raise ReleaseError()
                capability = object()
                session = ResolvedSyntheticSession(capability, seed.csrf, seed.principal)
                self._sessions[seed.token] = (seed, session)
                self._capabilities.append((capability, seed))
        except Exception:
            raise ReleaseError() from None

    @contextlib.contextmanager
    def _locked(self):
        try:
            private_directory(self.path)
            with attempt_lock(self.path):
                yield
        except Exception:
            raise ReleaseError() from None

    def _now(self):
        now = self.clock()
        if not _number(now):
            raise ReleaseError()
        return now

    def resolve(self, token: object) -> ResolvedSyntheticSession:
        try:
            if type(token) is not str:
                raise ReleaseError()
            seed, session = self._sessions[token]
            if seed.kind != 'synthetic_human' or self._now() >= seed.expires_at:
                raise ReleaseError()
            return session
        except Exception:
            raise ReleaseError() from None

    def _human(self, credential):
        for capability, seed in self._capabilities:
            if credential is capability and seed.kind == 'synthetic_human' and self._now() < seed.expires_at:
                return seed
        raise ReleaseError()

    def authenticate(self, credential: object, binding: dict) -> Approval:
        with self._locked():
            seed = self._human(credential)
            binding = _binding(binding, self.destinations)
            now = self._now()
            if not now < binding['expires_at'] <= now + 300:
                raise ReleaseError()
            if len(list(self.path.glob('receipt-*.json'))) >= MAX_RECEIPTS:
                raise ReleaseError()
            receipt = str(uuid.uuid4())
            record = {'version': 1, 'receipt': receipt, 'principal': seed.principal,
                      'binding': binding, 'session_expires_at': seed.expires_at}
            record['checksum'] = hashlib.sha256(_canonical(record)).hexdigest()
            _write_once(self.path/f'receipt-{receipt}.json', record)
            return Approval(seed.principal, receipt)

    def _record(self, approval, binding):
        if type(approval) is not Approval or not _receipt_id(approval.receipt) or not _identifier(approval.principal):
            raise ReleaseError()
        binding = _binding(binding, self.destinations)
        record = _read(self.path/f'receipt-{approval.receipt}.json')
        if record.keys() != {'version', 'receipt', 'principal', 'binding', 'session_expires_at', 'checksum'}:
            raise ReleaseError()
        check = {key: value for key, value in record.items() if key != 'checksum'}
        if (type(record['version']) is not int or record['version'] != 1
                or record['receipt'] != approval.receipt or record['principal'] != approval.principal
                or record['binding'] != binding or not _number(record['session_expires_at'])
                or record['checksum'] != hashlib.sha256(_canonical(check)).hexdigest()):
            raise ReleaseError()
        return record

    def verify(self, approval: Approval, binding: dict) -> bool:
        try:
            with self._locked():
                record = self._record(approval, binding)
                marker = self.path/f'revoked-{approval.receipt}.json'
                if marker.exists() or marker.is_symlink():
                    return False  # Even an interrupted/corrupt tombstone denies approval.
                return self._now() < min(record['binding']['expires_at'], record['session_expires_at'])
        except Exception:
            return False

    def revoke(self, approval: Approval, binding: dict) -> None:
        with self._locked():
            self._record(approval, binding)  # Expired receipts still need durable revocation.
            marker = self.path/f'revoked-{approval.receipt}.json'
            expected = {'version': 1, 'receipt': approval.receipt, 'revoked': True}
            if marker.exists() or marker.is_symlink():
                if _read(marker) != expected:
                    raise ReleaseError()
                return
            _write_once(marker, expected)
