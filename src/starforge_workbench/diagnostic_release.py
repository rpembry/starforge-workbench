"""Synthetic local review boundary; no network transport or host authentication.

Only an operator-owned composition root may construct this broker with an approval
verifier and credentials. A model's report is data, never an authorization request.
Caller-owned private files are not a sandbox against another process with the same
OS identity. The eventual Qwen identity must have no access to broker state, signing
material, or the operator review interface.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
import stat
from pathlib import Path
import time
from typing import Protocol
import uuid

from .docker_worker import atomic, attempt_lock, private_directory

DESTINATION = 'coordinator.synthetic'
MAX_PAYLOAD = 8192
STATUSES = frozenset({'queued', 'running', 'report_ready', 'failed', 'cancelled', 'uncertain'})


class ReleaseError(RuntimeError):
    def __init__(self):
        super().__init__('Release denied')


def automatic_status(status: str) -> dict:
    if type(status) is not str or status not in STATUSES:
        raise ReleaseError()
    return {'protocol': 'diagnostic.status.v1', 'status': status}


@dataclass(frozen=True)
class ReleasePolicy:
    """Explicit operator policy; arbitrary destinations are not supported."""
    destination: str
    automatic_statuses: frozenset[str]

    def __post_init__(self):
        if (self.destination != DESTINATION or type(self.automatic_statuses) is not frozenset
                or not self.automatic_statuses <= STATUSES):
            raise ReleaseError()


@dataclass(frozen=True)
class Approval:
    principal: str
    receipt: str


class ApprovalVerifier(Protocol):
    """Trusted local authenticator, supplied by owner, separate from model APIs.

    authenticate returns an authenticated operator principal and a fresh opaque durable
    receipt bound to every field of binding. verify must authenticate that receipt,
    including principal, on restart. revoke must durably invalidate the receipt in
    authority-owned state outside the mutable release journal, surviving restart.
    A bool or caller-claimed actor is insufficient.
    """
    def authenticate(self, credential: object, binding: dict) -> Approval: ...
    def verify(self, approval: Approval, binding: dict) -> bool: ...
    def revoke(self, approval: Approval, binding: dict) -> None: ...


@dataclass(frozen=True)
class ReviewSnapshot:
    job_id: str
    attempt_id: str
    operation_id: str
    revision: int
    destination: str
    payload: bytes

    def binding(self) -> dict:
        return {'protocol': 'diagnostic.release.v1', 'job_id': self.job_id,
                'attempt_id': self.attempt_id, 'operation_id': self.operation_id,
                'revision': self.revision, 'destination': self.destination,
                'payload': base64.b64encode(self.payload).decode('ascii'),
                'sha256': hashlib.sha256(self.payload).hexdigest()}


def _read(path: Path) -> dict:
    fd = None
    try:
        private_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
            raise ReleaseError()
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            content = stream.read(65537)
        if len(content) > 65536:
            raise ReleaseError()
        value = json.loads(content)
        if type(value) is not dict:
            raise ReleaseError()
        return value
    except Exception:
        raise ReleaseError() from None
    finally:
        if fd is not None:
            os.close(fd)


class FakeReleaseEndpoint:
    """Durable synthetic inbox and exact-operation reconciliation, never networking."""
    def __init__(self, path: Path):
        self.path = private_directory(path)
        self.fail_after_accept = False
        self.unknown = False

    def reconcile(self, binding: dict) -> str:
        with attempt_lock(self.path):
            if self.unknown:
                return 'unknown'
            ledger = _read(self.path/'inbox.json') if (self.path/'inbox.json').exists() else {}
            prior = ledger.get(binding['operation_id'])
            if prior is None:
                return 'absent'
            if prior != binding:
                raise ReleaseError()
            return 'accepted'

    def send(self, binding: dict, payload: bytes) -> None:
        with attempt_lock(self.path):
            if (binding['destination'] != DESTINATION or len(payload) > MAX_PAYLOAD
                    or base64.b64encode(payload).decode('ascii') != binding['payload']):
                raise ReleaseError()
            ledger = _read(self.path/'inbox.json') if (self.path/'inbox.json').exists() else {}
            if binding['operation_id'] in ledger:
                raise ReleaseError()
            ledger[binding['operation_id']] = binding
            if len(ledger) > 64 or len((json.dumps(ledger, sort_keys=True) + '\n').encode()) > 65536:
                raise ReleaseError()
            atomic(self.path/'inbox.json', ledger)
            if self.fail_after_accept:
                raise RuntimeError('Synthetic acknowledgement interruption')


class ReleaseBroker:
    """One existing attempt's local release owner; no scheduler or job ledger."""
    def __init__(self, attempt_path: Path, *, job_id: str, attempt_id: str,
                 authenticate: ApprovalVerifier, endpoint: FakeReleaseEndpoint,
                 policy: ReleasePolicy, ownership, clock=time.time):
        self.path = private_directory(attempt_path)
        if (type(endpoint) is not FakeReleaseEndpoint or endpoint.path == self.path
                or type(policy) is not ReleasePolicy or not job_id or not attempt_id
                or type(job_id) is not str or type(attempt_id) is not str
                or len(job_id) > 128 or len(attempt_id) > 128 or not callable(ownership)):
            raise ReleaseError()
        self.job_id, self.attempt_id = job_id, attempt_id
        self.authority, self.endpoint, self.policy, self.clock = authenticate, endpoint, policy, clock
        self.ownership = ownership

    def _save(self, state):
        private_directory(self.path)
        atomic(self.path/'release.json', state)

    def _load(self):
        state = _read(self.path/'release.json')
        try:
            snap = self._snapshot(state)
            if (state['version'] != 1 or snap.job_id != self.job_id or snap.attempt_id != self.attempt_id
                    or snap.destination != self.policy.destination or type(snap.revision) is not int
                    or snap.revision < 1 or len(snap.payload) > MAX_PAYLOAD
                    or str(uuid.UUID(snap.operation_id)) != snap.operation_id
                    or state['phase'] not in {'review', 'approved', 'rejected', 'deferred', 'cancelled',
                                              'intent', 'uncertain', 'released'}
                    or state['binding'] != snap.binding()):
                raise ReleaseError()
            return state
        except (KeyError, ValueError, TypeError):
            raise ReleaseError() from None

    def _snapshot(self, state):
        b = state['binding']
        return ReviewSnapshot(b['job_id'], b['attempt_id'], b['operation_id'], b['revision'],
                              b['destination'], base64.b64decode(b['payload'], validate=True))

    def prepare(self, payload: bytes, destination: str = DESTINATION) -> ReviewSnapshot:
        """Local edit/new draft invalidates prior approval; bytes never leave here."""
        if type(payload) is not bytes or len(payload) > MAX_PAYLOAD or destination != self.policy.destination:
            raise ReleaseError()
        with attempt_lock(self.path):
            self._require_active()
            previous = self._load() if (self.path/'release.json').exists() else None
            if previous and previous['phase'] in {'intent', 'uncertain', 'released', 'cancelled'}:
                raise ReleaseError()
            if previous:
                self._revoke_approval(previous)
            snap = ReviewSnapshot(self.job_id, self.attempt_id, str(uuid.uuid4()),
                                  previous['binding']['revision'] + 1 if previous else 1, destination, payload)
            self._save({'version': 1, 'binding': snap.binding(), 'phase': 'review'})
            return snap

    def review(self) -> ReviewSnapshot:
        """Trusted local UI only: display exact destination, revision and bytes."""
        with attempt_lock(self.path):
            return self._snapshot(self._load())

    def _approval_binding(self, state):
        return {**state['binding'], 'expires_at': state['expires_at']}

    def _require_active(self):
        try:
            marker = self.path/'release-cancel.json'
            if marker.exists() or marker.is_symlink():
                _read(marker)
                raise ReleaseError()
            if self.ownership() != 'active':
                raise ReleaseError()
        except Exception:
            raise ReleaseError() from None

    def _revoke_approval(self, state):
        if 'approval' not in state:
            return
        try:
            approval = Approval(**state['approval'])
            binding = {**self._approval_binding(state), 'decision': state['decision']}
            self.authority.revoke(approval, binding)
            if self.authority.verify(approval, binding) is not False:
                raise ReleaseError()
        except Exception:
            raise ReleaseError() from None

    def decide(self, snapshot: ReviewSnapshot, credential: object, *, decision='approve', ttl=300):
        if decision not in {'approve', 'reject', 'defer'} or type(ttl) not in {int, float} or not 0 < ttl <= 300:
            raise ReleaseError()
        with attempt_lock(self.path):
            state = self._load()
            if snapshot != self._snapshot(state) or state['phase'] not in {'review', 'deferred'}:
                raise ReleaseError()
            self._require_active()
            self._revoke_approval(state)
            state['expires_at'] = self.clock() + ttl
            binding = {**self._approval_binding(state), 'decision': decision}
            try:
                approval = self.authority.authenticate(credential, binding)
                if (type(approval) is not Approval or not approval.principal or not approval.receipt
                        or self.authority.verify(approval, binding) is not True):
                    raise ReleaseError()
            except Exception:
                raise ReleaseError() from None
            # The decision is part of the signed receipt, preventing reject -> approve tampering.
            state['approval'] = {'principal': approval.principal, 'receipt': approval.receipt}
            state['decision'] = decision
            state['phase'] = {'approve': 'approved', 'reject': 'rejected', 'defer': 'deferred'}[decision]
            self._save(state)

    def _dispatch_approval(self, state):
        # Verify the authenticated human decision as well as the exact content binding.
        try:
            approval = Approval(**state['approval'])
            return (state['decision'] == 'approve' and self.clock() < state['expires_at']
                    and math.isfinite(state['expires_at'])
                    and type(approval.principal) is str and bool(approval.principal)
                    and type(approval.receipt) is str and bool(approval.receipt)
                    and self.authority.verify(approval, {**self._approval_binding(state), 'decision': 'approve'}) is True)
        except Exception:
            return False

    def dispatch(self) -> dict:
        with attempt_lock(self.path):
            state = self._load()
            if state['phase'] in {'intent', 'uncertain'}:
                # Crash recovery only queries the exact durable operation; it never sends.
                try:
                    result = self.endpoint.reconcile(state['binding'])
                except Exception:
                    result = 'unknown'
                state['phase'] = 'released' if result == 'accepted' else 'uncertain'
                self._save(state)
                return self.status('report_ready' if result == 'accepted' else 'uncertain')
            self._require_active()
            self.status('report_ready'); self.status('uncertain')  # preflight response policy
            if state['phase'] != 'approved' or not self._dispatch_approval(state):
                raise ReleaseError()
            # Detect a rolled-back approved journal before handing any bytes to the endpoint.
            try:
                if self.endpoint.reconcile(state['binding']) != 'absent':
                    raise ReleaseError()
            except Exception:
                raise ReleaseError() from None
            self._require_active()
            state['phase'] = 'intent'
            self._save(state)  # fsynced intent precedes any fake side effect
            try:
                self.endpoint.send(state['binding'], self._snapshot(state).payload)
            except Exception:
                state['phase'] = 'uncertain'
                self._save(state)
                return self.status('uncertain')
            state['phase'] = 'released'
            self._save(state)
            return self.status('report_ready')

    def _invalidate(self, cancelled):
        with attempt_lock(self.path):
            state = self._load() if (self.path/'release.json').exists() else None
            if state and state['phase'] == 'released':
                raise ReleaseError()
            if state:
                self._revoke_approval(state)
            if cancelled:
                atomic(self.path/'release-cancel.json', {'version': 1, 'job_id': self.job_id,
                                                        'attempt_id': self.attempt_id})
            if state is None:
                if cancelled:
                    return
                raise ReleaseError()
            # An in-flight/crashed send cannot be recalled; retain uncertainty for reconciliation.
            if state['phase'] not in {'intent', 'uncertain'}:
                state['phase'] = 'cancelled' if cancelled else 'review'
            if not cancelled and state['phase'] == 'review':
                snapshot = self._snapshot(state)
                state['binding'] = ReviewSnapshot(self.job_id, self.attempt_id, str(uuid.uuid4()),
                                                 snapshot.revision + 1, snapshot.destination,
                                                 snapshot.payload).binding()
            state.pop('approval', None)
            state.pop('expires_at', None)
            state.pop('decision', None)
            self._save(state)

    def cancel(self):
        self._invalidate(True)

    def revoke(self):
        self._invalidate(False)

    def status(self, status: str) -> dict:
        if type(status) is not str or status not in self.policy.automatic_statuses:
            raise ReleaseError()
        return automatic_status(status)
