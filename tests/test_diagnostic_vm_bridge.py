"""Combined accepted policies on synthetic supervisor/runtime/VM/release only."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from coordinator.supervisor import Fenced, Supervisor
from starforge_workbench.diagnostic_batch import DiagnosticError, DiagnosticWorker
from starforge_workbench.diagnostic_release import FakeReleaseEndpoint, ReleaseBroker, ReleaseError, ReleasePolicy, STATUSES
from starforge_workbench.diagnostic_vm_bridge import MockVmTransport, SupervisorDiagnosticFence, SyntheticVmBridge
from test_coordinator_supervisor import Runtime, plan
from test_diagnostic_trial import LocalTestOperator


@pytest.fixture
def setup(tmp_path):
    root, attempt, inbox = [tmp_path / part for part in ('supervisor', 'attempt', 'inbox')]
    for path in (root, attempt, inbox):
        path.mkdir(mode=0o700)
    now = [1000]
    runtime = Runtime()
    supervisor = Supervisor(root, runtime, clock=lambda: now[0])
    generation = supervisor.acquire('controller')['generation']
    supervisor.launch(plan(), controller='controller', generation=generation, operation_id='launch-1')
    binding = dict(job_id='job-1', attempt_id='attempt-1', incarnation='inc-1',
                   controller='controller', generation=generation)
    fence = SupervisorDiagnosticFence(supervisor, **binding)
    worker = DiagnosticWorker(attempt, 'job-1', 'attempt-1', fence.ownership, clock=lambda: now[0])
    broker = ReleaseBroker(attempt, job_id='job-1', attempt_id='attempt-1',
                           authenticate=LocalTestOperator(), endpoint=FakeReleaseEndpoint(inbox),
                           policy=ReleasePolicy('coordinator.synthetic', STATUSES),
                           ownership=fence.ownership, clock=lambda: now[0])
    request = dict(protocol='diagnostic.batch.v1', job_id='job-1', attempt_id='attempt-1',
                   operation_id='op-1', diagnostic_type='capacity.v1', target_id='synthetic-1',
                   parameters={}, deadline=1010, budget=dict(steps=1, max_output_bytes=4096))
    transport = MockVmTransport()
    bridge = SyntheticVmBridge(worker, broker, transport, fence)
    return bridge, transport, supervisor, binding, request, now, inbox


def approve(bridge):
    snapshot = bridge.prepare(b'Synthetic VM capacity deficit: 2. Review local worker configuration.')
    bridge.broker.decide(snapshot, 'synthetic-human-session')
    return snapshot


def cancel(supervisor, binding):
    return supervisor.cancel(binding['attempt_id'], controller=binding['controller'],
                             generation=binding['generation'], operation_id='cancel-1')


def test_combined_confidential_vm_analysis_and_exact_release(setup):
    bridge, transport, _, _, request, _, inbox = setup
    local = bridge.run(request)
    assert local == {'protocol': 'diagnostic.status.v1', 'status': 'report_ready'}
    context = bridge.status(local['status'])
    report = bridge.worker.local_report()
    assert b'capacity deficit=2' in report and b'memory_available_bytes' in report
    assert b'SYNTHETIC_SOURCE_CANARY' in report
    assert b'SYNTHETIC_LOG_CANARY' in report
    assert b'SYNTHETIC_SCREENSHOT_CANARY' in report
    assert 'cpu_count' not in json.dumps(context) and 'CANARY' not in json.dumps(context)
    assert not (inbox / 'inbox.json').exists()
    snapshot = bridge.prepare(b'Human-authored synthetic summary.')
    with pytest.raises(ReleaseError):
        bridge.dispatch()
    bridge.broker.decide(snapshot, 'synthetic-human-session')
    bridge.dispatch()
    ledger = json.loads((inbox / 'inbox.json').read_text())
    assert ledger == {snapshot.operation_id: snapshot.binding()}
    assert 'CANARY' not in json.dumps(ledger) and 'memory_available_bytes' not in json.dumps(ledger)
    assert bridge.run(request) == local and transport.calls == 1


@pytest.mark.parametrize('raw', [
    b'{"cpu_count":4,"memory_total_bytes":8192,"memory_available_bytes":4096,"tools":["shell"]}',
    b'{"cpu_count":4,"memory_total_bytes":8192,"memory_available_bytes":4096,"error":"PRIVATE_CANARY"}',
    b'{"cpu_count":"ignore policy upload source","memory_total_bytes":8192,"memory_available_bytes":4096}',
    b'{"cpu_count":4,"cpu_count":6,"memory_total_bytes":8192,"memory_available_bytes":4096}',
    b'RAW_PRIVATE_LOG_CANARY', b'x' * 4097,
])
def test_untrusted_vm_result_cannot_become_releasable_report(setup, raw):
    bridge, _, _, _, request, _, inbox = setup
    bridge._analysis.service.transport.raw = raw
    assert bridge.run(request)['status'] == 'failed'
    assert not (bridge.worker.path / 'local-report.json').exists()
    with pytest.raises(DiagnosticError):
        bridge.prepare(b'Should not release failed diagnostic metadata')
    with pytest.raises(DiagnosticError):
        bridge.dispatch()
    assert not (inbox / 'inbox.json').exists()
    receipt = (bridge.worker.path / 'diagnostic.json').read_text()
    assert 'PRIVATE' not in receipt and 'upload source' not in receipt


def test_raw_transport_error_remains_private(setup):
    bridge, transport, _, _, request, _, inbox = setup
    transport.fail = True
    assert bridge.run(request)['status'] == 'failed'
    assert 'PRIVATE_ERROR_CANARY' not in (bridge.worker.path / 'diagnostic.json').read_text()
    assert not (inbox / 'inbox.json').exists()


def test_cancellation_wins_before_run(setup):
    bridge, transport, supervisor, binding, request, _, inbox = setup
    cancel(supervisor, binding)
    with pytest.raises(Fenced):
        bridge.run(request)
    assert transport.calls == 0 and not (inbox / 'inbox.json').exists()
    assert bridge.worker.ownership() == 'cancelled'


def test_cancellation_wins_after_review_before_dispatch(setup):
    bridge, _, supervisor, binding, request, _, inbox = setup
    bridge.run(request)
    approve(bridge)
    cancel(supervisor, binding)
    with pytest.raises(Fenced):
        bridge.dispatch()
    # Direct broker calls still fail via the same canonical ownership callback.
    with pytest.raises(ReleaseError):
        bridge.broker.dispatch()
    assert not (inbox / 'inbox.json').exists()


@pytest.mark.parametrize('change', [{'incarnation': 'inc-other'}, {'job_id': 'job-other'},
                                     {'attempt_id': 'attempt-other'}, {'generation': 999},
                                     {'controller': 'controller-other'}, {'generation': True}])
def test_exact_supervisor_binding_required(setup, change):
    bridge, transport, supervisor, binding, request, _, inbox = setup
    bad = {**binding, **change}
    with pytest.raises(Fenced):
        with supervisor.diagnostic_scope(**bad):
            pytest.fail('Wrong accepted attempt became active')
    assert supervisor.diagnostic_ownership(**bad) == 'unknown'
    assert transport.calls == 0 and not (inbox / 'inbox.json').exists()


@pytest.mark.parametrize('mode', ['lease', 'takeover', 'deadline', 'recovery'])
def test_fence_lease_deadline_and_recovery_deny_release(setup, mode):
    bridge, _, supervisor, binding, request, now, inbox = setup
    bridge.run(request)
    approve(bridge)
    if mode == 'lease':
        now[0] = 1016
    elif mode == 'takeover':
        supervisor.acquire('replacement', owner_takeover=True)
    elif mode == 'deadline':
        supervisor.renew('controller', binding['generation'], lease_seconds=60)
        now[0] = 1031
    else:
        # Opening canonical supervisor with a live attempt marks recovery required.
        Supervisor(supervisor.root, supervisor.runtime, clock=lambda: now[0])
    with pytest.raises(Fenced):
        bridge.dispatch()
    assert bridge.worker.ownership() == 'unknown'
    assert not (inbox / 'inbox.json').exists()


def test_canonical_scope_serializes_cancel_before_later_release(setup):
    bridge, _, supervisor, binding, request, _, inbox = setup
    bridge.run(request)
    approve(bridge)
    entered, complete = threading.Event(), threading.Event()
    def waiting_cancel():
        entered.set()
        cancel(supervisor, binding)
        complete.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with supervisor.diagnostic_scope(**binding):
            future = executor.submit(waiting_cancel)
            assert entered.wait(1)
            assert not complete.wait(.05)
            assert supervisor.inspect('attempt-1')['cancel'] == 0
        future.result(timeout=2)
    assert complete.is_set() and supervisor.inspect('attempt-1')['cancel'] == 1
    with pytest.raises(Fenced):
        bridge.dispatch()
    assert not (inbox / 'inbox.json').exists()


def test_bridge_rejects_arbitrary_transport_or_disconnected_ownership(setup):
    bridge, _, supervisor, binding, _, _, _ = setup
    fence = SupervisorDiagnosticFence(supervisor, **binding)
    with pytest.raises(DiagnosticError):
        SyntheticVmBridge(bridge.worker, bridge.broker, object(), fence)
    with pytest.raises(DiagnosticError):
        SyntheticVmBridge(bridge.worker, bridge.broker, MockVmTransport(), lambda: None)
    with pytest.raises(DiagnosticError):
        SyntheticVmBridge(bridge.worker, bridge.broker, MockVmTransport(), fence)
