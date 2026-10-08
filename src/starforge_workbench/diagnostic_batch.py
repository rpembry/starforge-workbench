"""Opt-in, synthetic-only local diagnostic attempt; never schedules or launches a model.

The supervisor owns allocation, cancellation and resource admission. Its trusted
local driver supplies an existing private attempt directory and an ownership
probe. The default fake adapter cannot enforce real model resources. The separate
explicit installed-model smoke adapter accepts only this fixed synthetic fixture
and owns its resource confinement; production/private-data integration is absent.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .docker_worker import atomic, attempt_lock, private_directory

PROTOCOL = 'diagnostic.batch.v1'
MAX_REPORT_BYTES = 16384
MAX_SECONDS = 300
STATES = frozenset(('queued', 'running', 'report_ready', 'failed', 'cancelled', 'uncertain'))
SYNTHETIC_FIXTURE = {'source': 'SYNTHETIC_SOURCE_CANARY',
                     'log': 'SYNTHETIC_LOG_CANARY',
                     'screenshot': 'SYNTHETIC_SCREENSHOT_CANARY', 'capacity': 4,
                     'configured_workers': 6}


class DiagnosticError(ValueError):
    """Fixed local error code, never model text or raw exception detail."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def local_status(status):
    """Local evidence only. Transport requires explicit release-broker policy."""
    if status not in STATES:
        raise DiagnosticError('invalid_status')
    return {'protocol': 'diagnostic.status.v1', 'status': status}


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)


class MockQwenAdapter:
    """Synthetic local Qwen stand-in: no shell, tools, paths, network or credentials."""
    def analyze(self, fixture):
        deficit = fixture['configured_workers'] - fixture['capacity']
        return {'summary': f'Synthetic configured workers exceed capacity by {deficit}.',
                'findings': [f'capacity deficit={deficit}', fixture['source'],
                             fixture['log'], fixture['screenshot']]}


class DiagnosticWorker:
    def __init__(self, attempt_path, job_id, attempt_id, ownership, clock=time.time):
        if not identifier(job_id) or not identifier(attempt_id) or not callable(ownership):
            raise DiagnosticError('invalid_owner')
        self.path = private_directory(Path(attempt_path))
        self.job_id, self.attempt_id = job_id, attempt_id
        self.ownership, self.clock = ownership, clock

    def _read(self, name, limit):
        path = self.path / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > limit):
                    raise DiagnosticError('unsafe_local_state')
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise DiagnosticError('unsafe_local_state')
                return data
        except DiagnosticError:
            raise
        except OSError:
            raise DiagnosticError('unavailable_local_state') from None

    def _receipt(self):
        try:
            receipt = json.loads(self._read('diagnostic.json', 4096))
            if (set(receipt) != {'protocol', 'job_id', 'attempt_id', 'operation_id',
                                  'digest', 'state', 'error_code', 'report_digest'}
                    or receipt['protocol'] != PROTOCOL or receipt['job_id'] != self.job_id
                    or receipt['attempt_id'] != self.attempt_id or receipt['state'] not in STATES):
                raise DiagnosticError('invalid_local_state')
            return receipt
        except (ValueError, TypeError, KeyError):
            raise DiagnosticError('invalid_local_state') from None

    def _ownership(self):
        try:
            state = self.ownership()
        except Exception:
            return 'unknown'
        return state if state in ('active', 'cancelled') else 'unknown'

    def _validate(self, request):
        keys = {'protocol', 'job_id', 'attempt_id', 'operation_id', 'diagnostic_type',
                'target_id', 'parameters', 'deadline', 'budget'}
        if not isinstance(request, dict) or set(request) != keys:
            raise DiagnosticError('invalid_request')
        if (request['protocol'] != PROTOCOL or request['job_id'] != self.job_id
                or request['attempt_id'] != self.attempt_id
                or not identifier(request['operation_id'])
                or request['diagnostic_type'] != 'capacity.v1'
                or request['target_id'] != 'synthetic-1'
                or type(request['parameters']) is not dict or request['parameters'] != {}):
            raise DiagnosticError('invalid_request')
        budget = request['budget']
        if (type(budget) is not dict or set(budget) != {'steps', 'max_output_bytes'}
                or type(budget['steps']) is not int or budget['steps'] != 1
                or type(budget['max_output_bytes']) is not int
                or not 128 <= budget['max_output_bytes'] <= MAX_REPORT_BYTES):
            raise DiagnosticError('invalid_budget')
        deadline = request['deadline']
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline > self.clock() + MAX_SECONDS):
            raise DiagnosticError('invalid_deadline')
        return hashlib.sha256(encoded(request)).hexdigest()

    def _finish(self, receipt, state, error=None):
        receipt.update(state=state, error_code=error)
        atomic(self.path / 'diagnostic.json', receipt)
        return local_status(state)

    def run(self, request, adapter):
        digest = self._validate(request)
        if not isinstance(adapter, MockQwenAdapter):
            raise DiagnosticError('synthetic_adapter_required')
        with attempt_lock(self.path):
            if (self.path / 'diagnostic.json').exists() or (self.path / 'diagnostic.json').is_symlink():
                receipt = self._receipt()
                if receipt['digest'] != digest or receipt['operation_id'] != request['operation_id']:
                    raise DiagnosticError('request_replay_mismatch')
                if receipt['state'] == 'running':
                    return self._finish(receipt, 'uncertain', 'interrupted_attempt')
                owner = self._ownership()
                if owner != 'active':
                    return self._finish(receipt, 'cancelled' if owner == 'cancelled'
                                        else 'uncertain', 'ownership_unavailable')
                return local_status(receipt['state'])
            receipt = {'protocol': PROTOCOL, 'job_id': self.job_id, 'attempt_id': self.attempt_id,
                       'operation_id': request['operation_id'], 'digest': digest, 'state': 'queued',
                       'error_code': None, 'report_digest': None}
            owner = self._ownership()
            if owner != 'active':
                return self._finish(receipt, 'cancelled' if owner == 'cancelled' else 'uncertain',
                                    'ownership_unavailable')
            if self.clock() >= request['deadline']:
                return self._finish(receipt, 'failed', 'deadline_exceeded')
            self._finish(receipt, 'running')  # durable intent before invoking even the mock
            try:
                result = adapter.analyze(dict(SYNTHETIC_FIXTURE))
                owner = self._ownership()
                if owner != 'active':
                    return self._finish(receipt, 'cancelled' if owner == 'cancelled' else 'uncertain',
                                        'ownership_unavailable')
                if self.clock() >= request['deadline']:
                    return self._finish(receipt, 'failed', 'deadline_exceeded')
                if (type(result) is not dict or set(result) != {'summary', 'findings'}
                        or type(result['summary']) is not str or len(result['summary']) > 4096
                        or type(result['findings']) is not list or len(result['findings']) > 16
                        or any(type(item) is not str or len(item) > 4096 for item in result['findings'])):
                    return self._finish(receipt, 'failed', 'invalid_model_result')
                report = (json.dumps(result, sort_keys=True) + '\n').encode()
                if len(report) > request['budget']['max_output_bytes']:
                    return self._finish(receipt, 'failed', 'output_limit')
                atomic(self.path / 'local-report.json', result)
                # Digest covers the exact persisted bytes presented to the local human.
                receipt['report_digest'] = hashlib.sha256(self._read('local-report.json', MAX_REPORT_BYTES + 128)).hexdigest()
                return self._finish(receipt, 'report_ready')
            except Exception:
                return self._finish(receipt, 'failed', 'adapter_failed')

    def local_report(self):
        """Trusted local human driver only; never an automatic cloud response."""
        with attempt_lock(self.path):
            receipt = self._receipt()
            if receipt['state'] != 'report_ready' or self._ownership() != 'active':
                raise DiagnosticError('report_unavailable')
            report = self._read('local-report.json', MAX_REPORT_BYTES + 128)
            if hashlib.sha256(report).hexdigest() != receipt['report_digest']:
                raise DiagnosticError('report_changed')
            return report
