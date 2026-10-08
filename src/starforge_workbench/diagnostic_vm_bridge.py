"""Trusted local composition of synthetic VM policy, analysis and human release.

No entry point is registered with MCP or the coordinator. The composition root
binds the existing supervisor's fenced scope and ownership probe; this bridge
has no scheduler, lease, cancellation ledger, guest access or live transport.
The scope must hold the canonical ownership/cancellation fence for each operation.
This in-process mock does not prove a real model or VM resource sandbox.
"""
import json
from types import MappingProxyType

from coordinator.supervisor import Supervisor

from workbench.vm_diagnostics import (
    Diagnostic, DiagnosticService, DisclosureProfile, GuestRegistration,
    StartupPolicy,
)

from .diagnostic_batch import DiagnosticError, DiagnosticWorker, MockQwenAdapter
from .diagnostic_release import ReleaseBroker, ReleaseError

SYNTHETIC_GUEST = 'g_' + '1' * 32
SYNTHETIC_CAPACITY = b'{"cpu_count":4,"memory_total_bytes":8192,"memory_available_bytes":4096}'


class MockVmTransport:
    """Synthetic bytes only; fixed diagnostic, no executable or guest connection."""
    def __init__(self, raw=SYNTHETIC_CAPACITY, *, fail=False):
        if type(raw) is not bytes or type(fail) is not bool:
            raise DiagnosticError('invalid_synthetic_transport')
        self.raw, self.fail, self.calls = raw, fail, 0

    def run(self, guest, operation, *, timeout_seconds, max_output_bytes):
        self.calls += 1
        if (guest.guest_id != SYNTHETIC_GUEST or guest.transport_ref != 'synthetic-local'
                or operation is not Diagnostic.CAPACITY or timeout_seconds != 5
                or max_output_bytes != 4096):
            raise DiagnosticError('invalid_synthetic_transport')
        if self.fail:
            raise RuntimeError('SYNTHETIC_PRIVATE_ERROR_CANARY')
        return self.raw


class _VmAnalysis(MockQwenAdapter):
    """No release authority: only the confidential local VM service is visible."""
    def __init__(self, service):
        self.service = service

    def analyze(self, fixture):
        result = self.service.execute(SYNTHETIC_GUEST, 'capacity')
        # Service failure metadata and raw bytes are never added to a report.
        if (type(result) is not dict or set(result) != {'schema_version', 'status', 'diagnostic', 'data'}
                or result['schema_version'] != 1 or result['status'] != 'ok'
                or result['diagnostic'] != 'capacity'):
            raise DiagnosticError('vm_diagnostic_failed')
        data = result['data']
        # The canonical VM schema validator has already checked these numeric fields.
        deficit = max(0, fixture['configured_workers'] - data['cpu_count'])
        return {'summary': f'Synthetic configured workers exceed VM CPU capacity by {deficit}.',
                'findings': [f'capacity deficit={deficit}',
                             'vm.capacity=' + json.dumps(data, sort_keys=True),
                             fixture['source'], fixture['log'], fixture['screenshot']]}


class SupervisorDiagnosticFence:
    """An existing supervisor is the sole authority; no second lifecycle state."""
    def __init__(self, supervisor, *, job_id, attempt_id, incarnation, controller, generation):
        if type(supervisor) is not Supervisor:
            raise DiagnosticError('invalid_bridge_owner')
        self.supervisor = supervisor
        self.binding = MappingProxyType(dict(job_id=job_id, attempt_id=attempt_id, incarnation=incarnation,
                                             controller=controller, generation=generation))
        self.ownership = lambda: supervisor.diagnostic_ownership(**self.binding)

    def scope(self):
        return self.supervisor.diagnostic_scope(**self.binding)


class SyntheticVmBridge:
    """Local driver only. Operator-authored payload review stays in ReleaseBroker."""
    def __init__(self, worker, broker, transport, fence):
        if (type(worker) is not DiagnosticWorker or type(broker) is not ReleaseBroker
                or type(transport) is not MockVmTransport or type(fence) is not SupervisorDiagnosticFence
                or worker.path != broker.path or worker.job_id != broker.job_id
                or worker.attempt_id != broker.attempt_id
                or worker.ownership is not broker.ownership or worker.ownership is not fence.ownership
                or worker.job_id != fence.binding['job_id']
                or worker.attempt_id != fence.binding['attempt_id']):
            raise DiagnosticError('invalid_bridge_owner')
        self.worker, self.broker, self.fenced_scope = worker, broker, fence.scope
        guest = GuestRegistration(SYNTHETIC_GUEST, 'synthetic-local', frozenset({Diagnostic.CAPACITY}))
        service = DiagnosticService(StartupPolicy(DisclosureProfile.CONFIDENTIAL), (guest,), transport)
        self._analysis = _VmAnalysis(service)

    def run(self, request):
        """Return local bounded status evidence, never VM fields or a transport send."""
        with self.fenced_scope():
            return self.worker.run(request, self._analysis)

    def prepare(self, payload):
        """Trusted human's explicit outbound draft; local VM report is never copied."""
        with self.fenced_scope():
            self.worker.local_report()  # require current ready evidence and ownership
            return self.broker.prepare(payload)

    def dispatch(self):
        """Exact approved payload only, serialized with canonical cancellation/fencing."""
        with self.fenced_scope():
            self.worker.local_report()
            return self.broker.dispatch()

    def status(self, status):
        """Only the broker's explicit operator status policy may cross the boundary."""
        with self.fenced_scope():
            return self.broker.status(status)
