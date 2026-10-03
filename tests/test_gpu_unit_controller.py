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
        self.processes = (ProcessGeneration(101, 1000), ProcessGeneration(102, 2000))
        self.remaining = self.processes
        self.alive = {101, 102}
        self.stops = 0
        self.starts = 0

    def snapshot(self):
        return UnitGeneration(self.unit, self.state, 'generation-one', self.cgroup,
                              101 if self.state == 'active' else 0,
                              self.processes if self.state == 'active' else ())

    def cgroup_processes(self, _cgroup):
        return self.remaining

    def generation_alive(self, process):
        return process.pid in self.alive

    def stop(self):
        self.stops += 1
        self.state = 'inactive'

    def start(self):
        self.starts += 1
        self.state = 'active'


class FakeGpu:
    def __init__(self):
        self.visible = {102, 909}  # Another app may use the GPU.

    def pids(self):
        return self.visible


class FakeScopes:
    def ended(self, _owner):
        return None


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
    gpu.visible = {909}  # Unrelated GPU context is allowed.
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
    unit.stop()
    unit.start()
    assert calls == [('systemctl', '--user', 'stop', 'fixture-idle.service'),
                     ('systemctl', '--user', 'start', 'fixture-idle.service')]
    with pytest.raises(ValueError):
        SystemdUserUnit('fixture-idle.service;other')


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
