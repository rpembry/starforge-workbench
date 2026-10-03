"""Synthetic integration: no systemd unit, miner, Ollama, or inference runs."""

import os
import threading

import pytest

from workbench.gpu_adapter import LocalAdapter
from workbench.gpu_isolated_request import OllamaServe, RequestUnavailable, SingleSyntheticRequest
from workbench.gpu_policy import Observation as O
from workbench.gpu_registry import Registry
from workbench.gpu_scope import ProcessGeneration as ScopeProcessGeneration
from workbench.gpu_scope import ScopeBinding, SupervisedScopeProbe
from workbench.gpu_start_gate import main as gate_main, start_permitted
from workbench.gpu_systemd_scope import SystemdRequestScopeSource
from workbench.gpu_unit_controller import ExactUnitController, ProcessGeneration, UnitGeneration
from test_gpu_unit_controller import FakeUnit


class FakeMiner:
    def __init__(self):
        self.process = self.context = O.PRESENT
        self.ended = False
        self.actions = []

    def observe(self, _leases):
        return self.process, self.context, True

    def workload_ended(self, _owner):
        return self.ended

    def apply(self, action):
        self.actions.append(action)
        self.process = self.context = (O.ABSENT if action == 'STOP' else O.PRESENT)


class ScopedLeaseClient:
    def __init__(self, adapter, owner):
        self.adapter, self.owner = adapter, owner

    def acquire(self, key, ttl):
        return self.adapter.acquire(self.owner, key, ttl)

    def status(self, token):
        return self.adapter.status(self.owner, token)

    def finish(self, token):
        return self.adapter.finish(self.owner, token)


class FakeProvider:
    def __init__(self, gate):
        self.gate = gate
        self.starts = 0
        self.generates = 0
        self.stops = 0
        self.stop_ok = True

    def start(self):
        assert not self.gate()
        self.starts += 1

    def generate(self):
        self.generates += 1

    def stop(self):
        self.stops += 1
        return self.stop_ok


def state(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'registry.sqlite'
    registry = Registry(path, 'boot-one')
    miner = FakeMiner()
    clock = [10.]
    adapter = LocalAdapter(registry, miner, clock_id='boot-one', clock=lambda: clock[0])
    return path, registry, miner, clock, adapter


def test_hold_before_start_and_finish_waits_for_whole_scope(tmp_path):
    path, registry, miner, clock, adapter = state(tmp_path)
    provider = FakeProvider(lambda: start_permitted(path))
    assert start_permitted(path)
    def progress(_seconds):
        assert provider.starts == 0
        assert not start_permitted(path)  # Durable acquire precedes stop.
        adapter.tick()  # Stop miner.
        adapter.tick()  # Confirm exit, grant.
    job = SingleSyntheticRequest(ScopedLeaseClient(adapter, 'scope-owner'), provider,
                                 clock=lambda: clock[0], sleep=progress)
    job.run('synthetic-one')
    assert miner.actions == ['STOP']
    assert (provider.starts, provider.generates) == (1, 1)
    assert not start_permitted(path)  # Finish does not drop an active scope.
    assert adapter.tick() == 'HOLD'
    miner.ended = True
    assert adapter.tick() == 'START'
    assert start_permitted(path)
    registry.close()


def test_unknown_exit_and_adapter_restart_keep_start_gate_closed(tmp_path):
    path, registry, miner, clock, adapter = state(tmp_path)
    lease = adapter.acquire('scope-owner', 'synthetic-two', 60)
    assert not start_permitted(path)
    adapter.tick()
    adapter.tick()
    adapter.finish('scope-owner', lease['token'])
    registry.close()
    assert not start_permitted(path)
    registry = Registry(path, 'boot-one')
    recovered = LocalAdapter(registry, miner, clock_id='boot-one', clock=lambda: clock[0])
    miner.ended = None
    assert recovered.tick() == 'HOLD'
    assert not start_permitted(path)
    miner.ended = True
    assert recovered.tick() == 'START'
    assert start_permitted(path)
    registry.close()


def test_scoped_legacy_release_cannot_bypass_whole_scope_end(tmp_path):
    path, registry, miner, _, adapter = state(tmp_path)
    owner = ScopeBinding('boot-one', '/user.slice/example-request.service',
                         'invocation-one', 501,
                         ScopeProcessGeneration(101, 1001)).owner_key()
    token = adapter.acquire(owner, 'synthetic-release', 60)['token']
    adapter.tick()
    adapter.tick()
    assert adapter.release(owner, token)['closing']
    assert not start_permitted(path)
    assert adapter.tick() == 'HOLD'
    miner.ended = True
    assert adapter.tick() == 'START'
    registry.close()


def test_gate_fails_closed_for_missing_bad_and_insecure_state(tmp_path):
    path, registry, _, _, _ = state(tmp_path)
    assert gate_main(['--registry', str(path)]) == 0
    assert gate_main(['--registry', str(path.with_name('missing.sqlite'))]) == 1
    os.chmod(path, 0o644)
    assert not start_permitted(path)
    os.chmod(path, 0o600)
    registry.db.execute('UPDATE policy SET snapshot=? WHERE id=1', ('invalid',))
    assert gate_main(['--registry', str(path)]) == 1
    registry.close()


def test_grant_timeout_never_starts_provider_and_retains_hold(tmp_path):
    path, registry, miner, clock, adapter = state(tmp_path)
    provider = FakeProvider(lambda: start_permitted(path))
    def progress(_seconds):
        clock[0] += 1
        miner.context = O.UNKNOWN
        adapter.tick()
    job = SingleSyntheticRequest(ScopedLeaseClient(adapter, 'scope-owner'), provider,
                                 clock=lambda: clock[0], sleep=progress,
                                 grant_timeout=2)
    with pytest.raises(RequestUnavailable, match='Miner exit'):
        job.run('synthetic-timeout')
    assert provider.starts == provider.generates == 0
    assert not start_permitted(path)
    miner.ended = True
    miner.context = O.ABSENT
    adapter.tick()
    assert start_permitted(path)
    registry.close()


def test_stop_unknown_retains_hold_until_scope_exit(tmp_path):
    path, registry, miner, clock, adapter = state(tmp_path)
    provider = FakeProvider(lambda: start_permitted(path))
    provider.stop_ok = False
    job = SingleSyntheticRequest(ScopedLeaseClient(adapter, 'scope-owner'), provider,
                                 clock=lambda: clock[0],
                                 sleep=lambda _seconds: (adapter.tick(), adapter.tick()))
    with pytest.raises(RequestUnavailable, match='did not stop'):
        job.run('synthetic-stop-unknown')
    miner.ended = None
    assert adapter.tick() == 'HOLD'
    assert not start_permitted(path)
    registry.close()


def test_grant_revocation_aborts_request_and_retains_hold(tmp_path):
    path, registry, miner, clock, adapter = state(tmp_path)
    class BlockingProvider(FakeProvider):
        def __init__(self):
            super().__init__(lambda: start_permitted(path))
            self.stopped = threading.Event()

        def generate(self):
            self.generates += 1
            miner.process = miner.context = O.PRESENT
            assert adapter.tick() == 'STOP'  # Unexpected miner restart.
            assert self.stopped.wait(2)

        def stop(self):
            self.stopped.set()
            return super().stop()

    provider = BlockingProvider()
    job = SingleSyntheticRequest(ScopedLeaseClient(adapter, 'scope-owner'), provider,
                                 clock=lambda: clock[0], watch_interval=0.01,
                                 sleep=lambda _seconds: (adapter.tick(), adapter.tick()))
    with pytest.raises(RequestUnavailable, match='grant was lost'):
        job.run('synthetic-revoked')
    assert provider.starts == provider.generates == 1
    assert provider.stopped.is_set()
    assert not start_permitted(path)
    registry.close()


def test_synthetic_ollama_request_requires_cached_model_and_bounded_body():
    provider = OllamaServe('example:small', 11435)
    calls = []
    def fake_json(path, payload=None, **_kwargs):
        calls.append((path, payload))
        return {'models': [{'name': 'example:small'}]} if path == '/api/tags' else {'done': True}
    provider._json = fake_json
    provider.generate()
    assert [path for path, _ in calls] == ['/api/tags', '/api/generate']
    assert calls[1][1] == {
        'model': 'example:small', 'prompt': 'Reply with exactly READY.',
        'stream': False, 'keep_alive': 0, 'options': {'num_predict': 8},
    }
    calls.clear()
    provider._json = lambda path, payload=None, **_kwargs: {'models': []}
    with pytest.raises(RequestUnavailable, match='existing cache'):
        provider.generate()


@pytest.mark.parametrize('moment', ['before-generate', 'after-generate'])
def test_synchronous_status_rejects_granted_but_stale_without_guard_tick(moment):
    class InconsistentLease:
        stale = False

        def acquire(self, _key, _ttl):
            return {'token': 'synthetic-token'}

        def status(self, _token):
            return {'token': 'synthetic-token', 'granted': True,
                    'stale': self.stale, 'expires_at_monotonic': 250.}

        def finish(self, _token):
            return {'closing': True}

    lease = InconsistentLease()
    class MutatingProvider(FakeProvider):
        def start(self):
            super().start()
            if moment == 'before-generate':
                lease.stale = True

        def generate(self):
            super().generate()
            if moment == 'after-generate':
                lease.stale = True

    provider = MutatingProvider(lambda: False)
    job = SingleSyntheticRequest(lease, provider, clock=lambda: 10.,
                                 watch_interval=5)
    with pytest.raises(RequestUnavailable, match='stale or expired'):
        job.run('synthetic-stale')
    assert provider.starts == 1
    assert provider.generates == (0 if moment == 'before-generate' else 1)
    assert provider.stops >= 1


@pytest.mark.parametrize('mutated', [
    {'token': 'other-token', 'granted': True, 'stale': False,
     'expires_at_monotonic': 250.},
    {'token': 'synthetic-token', 'granted': True, 'stale': False,
     'expires_at_monotonic': 10.},
    {'token': 'synthetic-token', 'granted': True, 'stale': None,
     'expires_at_monotonic': 250.},
])
def test_mismatched_expired_or_unknown_grant_never_starts_provider(mutated):
    class BadLease:
        def acquire(self, _key, _ttl):
            return {'token': 'synthetic-token'}

        def status(self, _token):
            return mutated

        def finish(self, _token):
            return {'closing': True}

    provider = FakeProvider(lambda: False)
    job = SingleSyntheticRequest(BadLease(), provider, clock=lambda: 10.)
    with pytest.raises(RequestUnavailable):
        job.run('synthetic-invalid')
    assert provider.starts == provider.generates == 0


class FakeRequestUnit:
    unit = 'example-request.service'

    def __init__(self, cgroup):
        self.cgroup = cgroup
        self.state = 'active'
        self.invocation = 'invocation-one'
        self.main_pid = 101
        self.members = (ProcessGeneration(101, 1001), ProcessGeneration(102, 1002))
        self.starts = {101: 1001, 102: 1002}

    def snapshot(self):
        return UnitGeneration(self.unit, self.state, self.invocation,
                              self.cgroup if self.state == 'active' else '',
                              self.main_pid if self.state == 'active' else 0,
                              self.members if self.state == 'active' else ())

    def cgroup_processes(self, _cgroup):
        return self.members

    def generation_alive(self, process):
        return self.starts.get(process.pid) == process.start_ticks


def test_systemd_scope_binding_members_teardown_and_restart_unknown(tmp_path):
    cgroup = '/user.slice/example-request.service'
    unit = FakeRequestUnit(cgroup)
    directory = tmp_path / 'user.slice' / unit.unit
    directory.mkdir(parents=True)
    source = SystemdRequestScopeSource(unit, cgroup_root=tmp_path,
                boot_reader=lambda: 'boot-one', start_ticks=lambda pid: unit.starts.get(pid),
                clock=lambda: 10.)
    owner = source.owner_for_peer(101, os.getuid(), os.getgid())
    binding = ScopeBinding.from_owner_key(owner)
    probe = SupervisedScopeProbe(source, clock=lambda: 10.5)
    assert probe.permitted_gpu_pids((type('Lease', (), {'owner': owner})(),)) == {101, 102}
    with pytest.raises(PermissionError):
        source.owner_for_peer(999, os.getuid(), os.getgid())
    unit.state = 'inactive'
    unit.members = ()
    unit.starts.clear()
    directory.rmdir()
    assert probe.ended(owner) is True
    unit.invocation = 'new-invocation'
    assert probe.ended(owner) is None
    assert source.observe(binding) is None


def test_incomplete_scope_evidence_never_claims_end(tmp_path):
    cgroup = '/user.slice/example-request.service'
    unit = FakeRequestUnit(cgroup)
    directory = tmp_path / 'user.slice' / unit.unit
    directory.mkdir(parents=True)
    source = SystemdRequestScopeSource(unit, cgroup_root=tmp_path,
                boot_reader=lambda: 'boot-one', start_ticks=lambda pid: unit.starts.get(pid),
                clock=lambda: 10.)
    owner = source.owner_for_peer(101, os.getuid(), os.getgid())
    unit.state = 'inactive'
    unit.members = ()
    unit.starts.clear()
    directory.rmdir()
    unit.members = None  # An unreadable recursive cgroup sample.
    assert SupervisedScopeProbe(source, clock=lambda: 10.5).ended(owner) is None


def test_full_fake_stack_exact_miner_and_request_scopes(tmp_path):
    cgroup = '/user.slice/example-request.service'
    request_unit = FakeRequestUnit(cgroup)
    request_unit.main_pid = 901
    request_unit.members = (ProcessGeneration(901, 9001),)
    request_unit.starts = {901: 9001}
    directory = tmp_path / 'user.slice' / request_unit.unit
    directory.mkdir(parents=True)
    source = SystemdRequestScopeSource(
        request_unit, cgroup_root=tmp_path, boot_reader=lambda: 'boot-one',
        start_ticks=lambda pid: request_unit.starts.get(pid), clock=lambda: 10.)
    owner = source.owner_for_peer(901, os.getuid(), os.getgid())
    probe = SupervisedScopeProbe(source, clock=lambda: 10.5)
    miner_unit = FakeUnit()
    class FakeGpu:
        visible = {102}

        def pids(self):
            return self.visible
    gpu = FakeGpu()
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'leases.sqlite'
    registry = Registry(path, 'boot-one')
    adapter = LocalAdapter(registry, ExactUnitController(miner_unit, gpu, probe),
                           clock_id='boot-one', clock=lambda: 10.)

    class ScopedProvider(FakeProvider):
        def start(self):
            super().start()
            assert miner_unit.state == 'inactive' and not gpu.visible
            request_unit.members += (ProcessGeneration(902, 9002),)
            request_unit.starts[902] = 9002
            gpu.visible = {902}

        def generate(self):
            super().generate()
            assert adapter.tick() == 'HOLD'

        def stop(self):
            request_unit.members = (ProcessGeneration(901, 9001),)
            request_unit.starts.pop(902, None)
            gpu.visible = set()
            return super().stop()

    provider = ScopedProvider(lambda: start_permitted(path))
    def progress(_seconds):
        assert adapter.tick() == 'STOP'
        miner_unit.remaining = ()
        miner_unit.alive.clear()
        gpu.visible = set()
        assert adapter.tick() == 'HOLD'

    SingleSyntheticRequest(ScopedLeaseClient(adapter, owner), provider,
                           clock=lambda: 10., sleep=progress).run('full-stack')
    assert not start_permitted(path) and miner_unit.starts == 0
    request_unit.state = 'inactive'
    request_unit.members = ()
    request_unit.starts.clear()
    directory.rmdir()
    assert adapter.tick() == 'START'
    assert start_permitted(path) and miner_unit.starts == 1
    registry.close()


def test_prechecked_start_cannot_grant_during_activation_or_pending_job(tmp_path):
    """The prior gate result alone is never accepted as current absence."""
    path, registry, _, _, _ = state(tmp_path)
    assert start_permitted(path)  # A start job checked the gate first.
    registry.close()
    registry = Registry(path, 'boot-one')
    unit = FakeUnit()
    class Gpu:
        visible = {102}

        def pids(self):
            return self.visible
    class Scopes:
        def ended(self, _owner):
            return False

        def permitted_gpu_pids(self, _leases):
            return set()
    gpu = Gpu()
    controller = ExactUnitController(unit, gpu, Scopes())
    adapter = LocalAdapter(registry, controller, clock_id='boot-one', clock=lambda: 10.)
    unit.state = 'activating'
    lease = adapter.acquire('scope-owner', 'activation-race', 60)
    assert not start_permitted(path)  # New gate checks cannot pass now.
    assert adapter.tick() == 'HOLD'
    assert not adapter.status('scope-owner', lease['token'])['granted']
    unit.state = 'active'
    assert adapter.tick() == 'STOP'
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    unit.start_job = True  # A queued/restarting job is also unknown.
    assert adapter.tick() == 'HOLD'
    unit.start_job = None  # Unparseable systemd Job property fails closed.
    assert adapter.tick() == 'HOLD'
    assert not adapter.status('scope-owner', lease['token'])['granted']
    unit.start_job = False
    assert adapter.tick() == 'HOLD'
    assert adapter.status('scope-owner', lease['token'])['granted']
    registry.close()
