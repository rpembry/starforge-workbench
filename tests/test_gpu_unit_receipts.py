"""Synthetic durable lifecycle receipts; no systemd unit is started."""

import os

import pytest

from workbench.gpu_policy import Observation as O
from workbench.gpu_scope import SupervisedScopeProbe
from workbench.gpu_systemd_scope import SystemdRequestScopeSource
from workbench.gpu_unit_controller import ExactUnitController
from workbench.gpu_unit_receipts import ReceiptError, UnitReceiptLedger, main
from test_gpu_isolated_request import FakeRequestUnit
from test_gpu_unit_controller import FakeGpu, FakeScopes, FakeUnit


FIRST = 'a' * 32
SECOND = 'b' * 32


def ledger_at(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'unit-receipts.sqlite'
    return path, UnitReceiptLedger(path, boot_reader=lambda: 'boot-one')


def test_capture_and_completion_survive_adapter_restart(tmp_path):
    path, ledger = ledger_at(tmp_path)
    unit, gpu = FakeUnit(), FakeGpu()
    unit.invocation = FIRST
    ledger.record_start(unit.unit, FIRST)
    controller = ExactUnitController(unit, gpu, FakeScopes(), receipts=ledger)
    controller.apply('STOP')
    captured = ledger.latest_capture(unit.unit)
    assert captured is not None and captured.invocation == FIRST
    unit.invocation = ''  # Observed user-systemd inactive form.
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    assert controller.observe()[:2] == (O.UNKNOWN, O.UNKNOWN)  # No end receipt.
    ledger.record_end(unit.unit, FIRST)
    reopened = UnitReceiptLedger(path, boot_reader=lambda: 'boot-one')
    recovered = ExactUnitController(unit, gpu, FakeScopes(), receipts=reopened)
    assert recovered.observe()[:2] == (O.ABSENT, O.ABSENT)
    ledger.record_start(unit.unit, SECOND)  # A replacement, even if now gone.
    assert recovered.observe()[:2] != (O.ABSENT, O.ABSENT)


def test_crash_after_durable_capture_before_stop_can_retry_exact_generation(tmp_path):
    path, ledger = ledger_at(tmp_path)
    unit, gpu = FakeUnit(), FakeGpu()
    unit.invocation = FIRST
    ledger.record_start(unit.unit, FIRST)
    captured = unit.snapshot()
    ledger.record_capture(captured)  # Crash before the pidfd stop effect.
    recovered = ExactUnitController(
        unit, gpu, FakeScopes(),
        receipts=UnitReceiptLedger(path, boot_reader=lambda: 'boot-one'))
    assert recovered.observe()[:2] == (O.PRESENT, O.UNKNOWN)
    recovered.apply('STOP')  # Identical captured generation is idempotent.
    assert unit.stops == 1
    unit.invocation = ''
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    ledger.record_end(unit.unit, FIRST)
    assert recovered.observe()[:2] == (O.ABSENT, O.ABSENT)


def test_replacement_start_between_receipt_samples_revokes_exit_proof(tmp_path, monkeypatch):
    _, ledger = ledger_at(tmp_path)
    unit, gpu = FakeUnit(), FakeGpu()
    unit.invocation = FIRST
    ledger.record_start(unit.unit, FIRST)
    controller = ExactUnitController(unit, gpu, FakeScopes(), receipts=ledger)
    controller.apply('STOP')
    unit.invocation = ''
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    ledger.record_end(unit.unit, FIRST)
    completed = ledger.completed
    calls = 0
    def racing_receipt(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = completed(*args, **kwargs)
        if calls == 1:
            ledger.record_start(unit.unit, SECOND)
        return result
    monkeypatch.setattr(ledger, 'completed', racing_receipt)
    assert controller.observe()[:2] == (O.ABSENT, O.UNKNOWN)


def test_unknown_cgroup_inode_or_member_keeps_exit_unverified(tmp_path):
    _, ledger = ledger_at(tmp_path)
    unit, gpu = FakeUnit(), FakeGpu()
    unit.invocation = FIRST
    ledger.record_start(unit.unit, FIRST)
    controller = ExactUnitController(unit, gpu, FakeScopes(), receipts=ledger)
    controller.apply('STOP')
    ledger.record_end(unit.unit, FIRST)
    unit.invocation = ''
    unit.remaining = ()
    unit.alive.clear()
    gpu.visible = set()
    unit.inode = 999  # Same cgroup path, different generation.
    unit.remaining = unit.processes
    assert controller.observe()[:2] != (O.ABSENT, O.ABSENT)
    unit.remaining = ()
    assert controller.observe()[:2] == (O.ABSENT, O.ABSENT)


def test_request_scope_completion_uses_receipt_after_inactive_id_clears(tmp_path):
    cgroup = '/user.slice/example-request.service'
    unit = FakeRequestUnit(cgroup)
    unit.invocation = FIRST
    directory = tmp_path / 'user.slice' / unit.unit
    directory.mkdir(parents=True)
    path, ledger = ledger_at(tmp_path)
    ledger.record_start(unit.unit, FIRST)
    source = SystemdRequestScopeSource(
        unit, cgroup_root=tmp_path, boot_reader=lambda: 'boot-one',
        start_ticks=lambda pid: unit.starts.get(pid), clock=lambda: 10.,
        receipts=ledger)
    owner = source.owner_for_peer(unit.main_pid, os.getuid(), os.getgid())
    unit.state = 'inactive'
    unit.invocation = ''
    unit.members = ()
    unit.starts.clear()
    directory.rmdir()
    probe = SupervisedScopeProbe(source, clock=lambda: 10.5)
    assert probe.ended(owner) is None
    ledger.record_end(unit.unit, FIRST)
    recovered = SystemdRequestScopeSource(
        unit, cgroup_root=tmp_path, boot_reader=lambda: 'boot-one',
        start_ticks=lambda pid: unit.starts.get(pid), clock=lambda: 10.,
        receipts=UnitReceiptLedger(path, boot_reader=lambda: 'boot-one'))
    assert SupervisedScopeProbe(recovered, clock=lambda: 10.5).ended(owner) is True
    ledger.record_start(unit.unit, SECOND)
    assert SupervisedScopeProbe(recovered, clock=lambda: 10.5).ended(owner) is None


def test_duplicate_missing_invalid_and_other_boot_receipts_fail_closed(tmp_path):
    path, ledger = ledger_at(tmp_path)
    unit = FakeUnit()
    unit.invocation = FIRST
    ledger.record_start(unit.unit, FIRST)
    with pytest.raises(ReceiptError):
        ledger.record_start(unit.unit, FIRST)
    with pytest.raises(ReceiptError):
        ledger.record_end(unit.unit, 'not-systemd-id')
    ledger.record_capture(unit.snapshot())
    ledger.record_end(unit.unit, FIRST)
    assert ledger.completed(unit.unit, FIRST, captured=unit.snapshot())
    next_boot = UnitReceiptLedger(path, boot_reader=lambda: 'next-boot')
    assert next_boot.latest_capture(unit.unit) is None
    assert not next_boot.completed(unit.unit, FIRST)
    ledger.record_start(unit.unit, SECOND)
    ledger.record_end(unit.unit, SECOND)
    assert not ledger.completed(unit.unit, FIRST, captured=unit.snapshot())


def test_end_without_start_or_capture_is_not_completion(tmp_path):
    _, ledger = ledger_at(tmp_path)
    ledger.record_end('fixture-idle.service', FIRST)
    assert not ledger.completed('fixture-idle.service', FIRST)
    assert ledger.latest_capture('fixture-idle.service') is None


def test_hook_requires_supervisor_invocation_and_private_path(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'unit-receipts.sqlite'
    ledger = UnitReceiptLedger(path)
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    args = ['--ledger', str(path), '--unit', 'fixture-idle.service', '--event', 'start']
    assert main(args) == 1
    monkeypatch.setenv('INVOCATION_ID', FIRST)
    assert main(args) == 0
    assert ledger.started('fixture-idle.service', FIRST)
    os.chmod(path, 0o644)
    assert main(['--ledger', str(path), '--unit', 'fixture-idle.service',
                 '--event', 'end']) == 1
