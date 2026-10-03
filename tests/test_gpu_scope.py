from dataclasses import replace

import pytest

from workbench.gpu_policy import Lease, Observation as O, ReservationPolicy
from workbench.gpu_scope import (
    ProcessGeneration, ScopeBinding, ScopeObservation, SupervisedScopeProbe,
)
from workbench.gpu_unit_controller import ExactUnitController
from test_gpu_unit_controller import FakeGpu, FakeUnit


class FakeSource:
    def __init__(self, boot='boot-one'):
        self.boot = boot
        self.observations = {}
        self.starts = {}

    def boot_id(self):
        return self.boot

    def observe(self, binding):
        return self.observations.get(binding.scope_id)

    def process_start_ticks(self, pid):
        return self.starts.get(pid)

    def record(self, binding, members, *, at=10, ended=False):
        self.observations[binding.scope_id] = ScopeObservation(
            binding, at, tuple(members), ended)
        self.starts.update((member.pid, member.start_ticks) for member in members)


def binding(scope, inode, pid, start):
    return ScopeBinding('boot-one', scope, 'generation-' + scope, inode,
                        ProcessGeneration(pid, start))


def test_binding_is_canonical_and_recoverable_from_persisted_lease():
    scope = binding('scope-a', 101, 11, 1000)
    key = scope.owner_key()
    assert ScopeBinding.from_owner_key(key) == scope
    with pytest.raises(ValueError):
        ScopeBinding.from_owner_key('caller-chosen-name')
    policy = ReservationPolicy(clock_id='boot-one')
    lease = policy.acquire(key, 'request-one', 0, 60)
    recovered = ReservationPolicy(policy.snapshot(), clock_id='boot-one')
    assert ScopeBinding.from_owner_key(recovered.view(key, lease.token).owner) == scope
    source = FakeSource()
    source.record(scope, (ProcessGeneration(11, 1000),))
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).permitted_gpu_pids(
        recovered.snapshot().leases) == {11}
    source.boot = 'next-boot'
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).permitted_gpu_pids(
        recovered.snapshot().leases) is None


def test_active_overlap_only_permits_fresh_matching_scope_generations():
    a, b, unused = binding('scope-a', 101, 11, 1000), binding('scope-b', 202, 21, 2000), binding('unused', 303, 31, 3000)
    source = FakeSource()
    source.record(a, (ProcessGeneration(11, 1000), ProcessGeneration(12, 1200)))
    source.record(b, (ProcessGeneration(21, 2000),))
    source.record(unused, (ProcessGeneration(31, 3000),))
    probe = SupervisedScopeProbe(source, clock=lambda: 10.5)
    leases = (Lease(a.owner_key(), 'token-a', 60), Lease(b.owner_key(), 'token-b', 60))
    assert probe.permitted_gpu_pids(leases) == {11, 12, 21}
    assert 31 not in probe.permitted_gpu_pids(leases)
    source.starts[12] = 1300  # PID reused after supervisor sampling.
    assert probe.permitted_gpu_pids(leases) is None
    source.starts[12] = 1200
    source.observations['scope-b'] = replace(source.observations['scope-b'],
                                             binding=replace(b, cgroup_inode=999))
    assert probe.permitted_gpu_pids(leases) is None


def test_stale_unknown_boot_and_crash_recovery_fail_closed():
    scope = binding('scope-a', 101, 11, 1000)
    source = FakeSource()
    source.record(scope, (ProcessGeneration(11, 1000),), at=10)
    lease = Lease(scope.owner_key(), 'token-a', 60)
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).permitted_gpu_pids((lease,)) == {11}
    assert SupervisedScopeProbe(source, clock=lambda: 12).permitted_gpu_pids((lease,)) is None
    source.boot = 'next-boot'
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).permitted_gpu_pids((lease,)) is None
    source.boot = 'boot-one'
    source.observations.clear()
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).permitted_gpu_pids((lease,)) is None
    source.record(scope, (), at=10, ended=True)
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).ended(scope.owner_key()) is True


def test_late_unbound_gpu_child_and_changed_gpu_sample_remain_unknown():
    scope = binding('scope-a', 101, 909, 9000)
    source = FakeSource()
    source.record(scope, (ProcessGeneration(909, 9000),))
    probe = SupervisedScopeProbe(source, clock=lambda: 10.5)
    unit, gpu = FakeUnit(), FakeGpu()
    controller = ExactUnitController(unit, gpu, probe)
    lease = Lease(scope.owner_key(), 'token-a', 60)
    controller.apply('STOP')
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = {909, 103}  # Child appeared after capture, outside bound scope.
    assert controller.observe((lease,))[:2] == (O.ABSENT, O.UNKNOWN)
    gpu.visible = {909}
    assert controller.observe((lease,))[:2] == (O.ABSENT, O.ABSENT)
    samples = iter(({909}, {909, 103}))
    gpu.pids = lambda: next(samples)
    assert controller.observe((lease,))[:2] == (O.ABSENT, O.UNKNOWN)
