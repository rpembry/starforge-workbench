import os
import signal
import subprocess

import pytest

from workbench.gpu_adapter import LocalAdapter
from workbench.gpu_policy import Observation as O
from workbench.gpu_registry import Registry
from workbench.gpu_unit_controller import (
    EvidenceUnavailable, ExactUnitController, ProcessGeneration,
    RocmKfdProcesses, SystemdUserUnit, UnitGeneration,
)


class FakeUnit:
    unit = 'fixture-idle.service'
    cgroup = '/user.slice/user-1000.slice/app.slice/fixture-idle.service'

    def __init__(self):
        self.state = 'active'
        self.invocation = 'generation-one'
        self.processes = (ProcessGeneration(101, 1000), ProcessGeneration(102, 2000))
        self.remaining = self.processes
        self.alive = {101, 102}
        self.stops = 0
        self.starts = 0
        self.replace_on_stop = False

    def snapshot(self):
        return UnitGeneration(self.unit, self.state, self.invocation, self.cgroup,
                              self.processes[0].pid if self.state == 'active' else 0,
                              self.processes if self.state == 'active' else ())

    def cgroup_processes(self, _cgroup):
        return self.remaining

    def generation_alive(self, process):
        return process.pid in self.alive

    def stop(self, expected):
        if self.replace_on_stop:
            self.replace_on_stop = False
            self.invocation = 'generation-two'
            self.processes = (ProcessGeneration(201, 3000), ProcessGeneration(202, 4000))
            self.remaining = self.processes
            self.alive = {201, 202}
            raise EvidenceUnavailable('Unit invocation changed before exact stop')
        if expected.invocation != self.invocation:
            raise EvidenceUnavailable('Wrong unit invocation')
        self.stops += 1
        self.state = 'inactive'

    def start(self):
        self.starts += 1
        self.state = 'active'


class FakeGpu:
    def __init__(self):
        self.visible = {102, 909}  # 909 belongs to another supervised scope.

    def pids(self):
        return self.visible


class FakeScopes:
    def __init__(self):
        self.permitted = {909}

    def ended(self, _owner):
        return None

    def permitted_gpu_pids(self):
        return self.permitted


def test_exact_stop_requires_full_cgroup_generation_and_gpu_context_exit(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    registry = Registry(private / 'state.sqlite', 'boot-one')
    unit, gpu = FakeUnit(), FakeGpu()
    controller = ExactUnitController(unit, gpu, FakeScopes())
    adapter = LocalAdapter(registry, controller, clock_id='boot-one', clock=lambda: 0.)
    acquired = adapter.acquire('supervised-scope', 'request-one', 60)
    assert not acquired['granted']
    assert adapter.tick() == 'STOP'
    assert unit.stops == 1 and unit.starts == 0
    assert adapter.tick() == 'STOP'  # Cgroup still has the child.
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    unit.remaining = ()
    unit.alive.clear()
    assert adapter.tick() == 'STOP'  # Captured miner GPU PID persists.
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    gpu.visible = {909}  # A positively attributed scope may use the GPU.
    assert adapter.tick() == 'HOLD'
    assert adapter.status('supervised-scope', acquired['token'])['granted']
    adapter.release('supervised-scope', acquired['token'])
    assert adapter.tick() == 'START'
    assert unit.starts == 1
    registry.close()


def test_unknown_gpu_or_lost_capture_never_grants(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    registry = Registry(private / 'state.sqlite', 'boot-one')
    unit, gpu = FakeUnit(), FakeGpu()
    adapter = LocalAdapter(registry, ExactUnitController(unit, gpu, FakeScopes()),
                           clock_id='boot-one', clock=lambda: 0.)
    acquired = adapter.acquire('supervised-scope', 'request-one', 60)
    adapter.tick()
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = None
    assert adapter.tick() == 'HOLD'
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    registry.close()
    registry = Registry(private / 'state.sqlite', 'boot-one')
    restarted = LocalAdapter(registry, ExactUnitController(unit, gpu, FakeScopes()),
                             clock_id='boot-one', clock=lambda: 0.)
    gpu.visible = set()
    assert restarted.tick() == 'HOLD'  # No captured generation after restart.
    assert not restarted.status('supervised-scope', acquired['token'])['granted']
    registry.close()


def test_replacement_invocation_during_stop_cannot_be_mistaken_for_old_exit(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    registry = Registry(private / 'state.sqlite', 'boot-one')
    unit, gpu = FakeUnit(), FakeGpu()
    controller = ExactUnitController(unit, gpu, FakeScopes())
    adapter = LocalAdapter(registry, controller, clock_id='boot-one', clock=lambda: 0.)
    acquired = adapter.acquire('supervised-scope', 'request-one', 60)
    unit.replace_on_stop = True
    with pytest.raises(EvidenceUnavailable, match='changed'):
        adapter.tick()
    assert unit.stops == 0 and unit.invocation == 'generation-two'
    gpu.visible.add(202)
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    assert adapter.tick() == 'STOP'  # Captures B separately; never signaled it as A.
    assert unit.stops == 1
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible.discard(102)
    assert adapter.tick() == 'STOP'  # B still owns a GPU context.
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    gpu.visible.discard(202)
    assert adapter.tick() == 'HOLD'
    assert adapter.status('supervised-scope', acquired['token'])['granted']
    registry.close()


def test_late_escaped_gpu_child_blocks_grant_until_context_gone(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    registry = Registry(private / 'state.sqlite', 'boot-one')
    unit, gpu, scopes = FakeUnit(), FakeGpu(), FakeScopes()
    adapter = LocalAdapter(registry, ExactUnitController(unit, gpu, scopes),
                           clock_id='boot-one', clock=lambda: 0.)
    acquired = adapter.acquire('supervised-scope', 'request-one', 60)
    assert adapter.tick() == 'STOP'
    # A new child appears after capture, leaves the old unit cgroup, and
    # retains KFD context. It was never among the originally captured PIDs.
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = {103, 909}
    assert adapter.tick() == 'HOLD'
    assert not adapter.status('supervised-scope', acquired['token'])['granted']
    gpu.visible = {909}
    scopes.permitted = None
    assert adapter.tick() == 'HOLD'  # Scope evidence itself is required.
    scopes.permitted = {909}
    assert adapter.tick() == 'HOLD'
    assert adapter.status('supervised-scope', acquired['token'])['granted']
    registry.close()


def test_changed_inactive_invocation_stays_unknown():
    unit, gpu = FakeUnit(), FakeGpu()
    controller = ExactUnitController(unit, gpu, FakeScopes())
    controller.apply('STOP')
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    unit.invocation = 'generation-two'
    assert controller.observe()[:2] == (O.UNKNOWN, O.UNKNOWN)


def test_exact_unit_rejects_mismatched_generation_and_other_actions():
    unit = FakeUnit()
    controller = ExactUnitController(unit, FakeGpu(), FakeScopes())
    with pytest.raises(EvidenceUnavailable):
        controller.apply('START')
    with pytest.raises(ValueError):
        controller.apply('KILL')
    unit.snapshot = lambda: UnitGeneration('other.service', 'active', 'wrong', unit.cgroup,
                                           101, unit.processes)
    with pytest.raises(EvidenceUnavailable):
        controller.apply('STOP')
    assert unit.stops == 0


def test_systemd_backend_uses_fixed_user_unit_argv(monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    unit = SystemdUserUnit('fixture-idle.service')
    unit.start()
    assert calls == [('systemctl', '--user', 'start', 'fixture-idle.service')]
    with pytest.raises(ValueError):
        SystemdUserUnit('fixture-idle.service;other')


def test_systemd_stop_signals_only_captured_main_pidfd(monkeypatch):
    class FakePidfd:
        def __init__(self):
            self.opened = []
            self.signals = []

        def open(self, pid):
            assert pid == 101
            fd = os.open('/dev/null', os.O_RDONLY)
            self.opened.append(fd)
            return fd

        def send(self, handle, sig):
            self.signals.append((handle, sig))

    ops = FakePidfd()
    unit = SystemdUserUnit('fixture-idle.service', pidfd=ops)
    captured = UnitGeneration(unit.unit, 'active', 'generation-one',
                              '/user.slice/user-1000.slice/app.slice/fixture-idle.service',
                              101, (ProcessGeneration(101, 1000), ProcessGeneration(102, 2000)))
    monkeypatch.setattr(unit, 'snapshot', lambda: captured)
    monkeypatch.setattr(unit, '_start_ticks', lambda _pid: 1000)
    unit.stop(captured)
    assert ops.signals == [(ops.opened[0], signal.SIGTERM)]
    changed = UnitGeneration(unit.unit, 'active', 'generation-two', captured.cgroup,
                             201, (ProcessGeneration(201, 3000),))
    monkeypatch.setattr(unit, 'snapshot', lambda: changed)
    with pytest.raises(EvidenceUnavailable, match='changed'):
        unit.stop(captured)
    assert ops.signals == [(ops.opened[0], signal.SIGTERM)]
    states = iter((captured, changed))
    monkeypatch.setattr(unit, 'snapshot', lambda: next(states))
    with pytest.raises(EvidenceUnavailable, match='after process binding'):
        unit.stop(captured)
    assert len(ops.opened) == 2 and len(ops.signals) == 1


def test_cgroup_probe_includes_nested_child_scope(tmp_path, monkeypatch):
    unit = SystemdUserUnit('fixture-idle.service', cgroup_root=tmp_path)
    directory = tmp_path / 'user.slice/user-1000.slice/app.slice/fixture-idle.service'
    child = directory / 'child.scope'
    child.mkdir(parents=True)
    (directory / 'cgroup.procs').write_text('101\n')
    (child / 'cgroup.procs').write_text('102\n')
    monkeypatch.setattr(unit, '_start_ticks', lambda pid: {101: 1000, 102: 2000}[pid])
    group = '/user.slice/user-1000.slice/app.slice/fixture-idle.service'
    assert unit.cgroup_processes(group) == (
        ProcessGeneration(101, 1000), ProcessGeneration(102, 2000))
    assert unit.cgroup_processes('/user.slice/other.service') is None


def test_rocm_kfd_parser_returns_unknown_on_missing_evidence(monkeypatch):
    output = ('KFD process information:\nPID PROCESS NAME GPU(s) VRAM USED\n'
              '102 miner 1 3500000000\n909 desktop 1 1000\n'
              '==== Memory Usage (Bytes) ====')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=output))
    assert RocmKfdProcesses().pids() == {102, 909}
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout='GPU busy: 100%'))
    assert RocmKfdProcesses().pids() is None
