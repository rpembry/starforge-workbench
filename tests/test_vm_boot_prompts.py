"""Explicit controlled readiness records; no VM/image/security operations."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import sys

import pytest

from workbench.vm_diagnostics import Diagnostic, GuestRegistration
from workbench.vm_sandbox import SyntheticVMPolicy
from workbench.vm_synthetic import BootDetector, BootProtocol, SyntheticConsoleTransport


PROTOCOL = BootProtocol('synthetic-guest')
LOGIN = b'SF_BOOT_V1 synthetic-guest login_ready\n'
SHELL = b'SF_BOOT_V1 synthetic-guest shell_ready\n'
AUTH = b'SF_BOOT_V1 synthetic-guest auth_required\n'


def events_at_splits(raw, boundaries):
    detector = BootDetector(PROTOCOL)
    events = []
    for end in boundaries:
        for event in detector.observe(raw[:end]):
            events.append(event)
    return events


@pytest.mark.parametrize('history', [
    b'Last failed login: \r\n', b'Last login: \n', b'Password: historical text\r\n',
    b'synthetic-guest login: historical example\n', b'synthetic-guest login: \n',
    b'Password: \n', b'root@synthetic-guest:~# historical example\n',
    b'\x1b[?2004hroot@synthetic-guest:~# \n',
    b'log SF_BOOT_V1 synthetic-guest login_ready\n',
])
def test_history_and_lookalikes_are_invariant_at_every_split_and_byte(history):
    raw = history + LOGIN + b'root\r\n' + history + SHELL
    assert events_at_splits(raw, [len(raw)]) == ['login','ready']
    for split in range(1, len(raw)):
        assert events_at_splits(raw, [split,len(raw)]) == ['login','ready']
    assert events_at_splits(raw, range(1,len(raw)+1)) == ['login','ready']
    assert events_at_splits(history, range(1,len(history)+1)) == []


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
def test_readiness_requires_complete_record_and_order_at_every_split(ending):
    login, shell = LOGIN.rstrip(b'\n')+ending, SHELL.rstrip(b'\n')+ending
    detector = BootDetector(PROTOCOL)
    assert list(detector.observe(login[:-1])) == []
    assert list(detector.observe(login)) == ['login']
    assert list(detector.observe(login)) == []
    assert list(detector.observe(login+shell[:-1])) == []
    assert list(detector.observe(login+shell)) == ['ready']
    raw = login+shell
    for split in range(1,len(raw)):
        assert events_at_splits(raw,[split,len(raw)]) == ['login','ready']


@pytest.mark.parametrize('bad', [AUTH, LOGIN, b'SF_BOOT_V1 other-host shell_ready\n',
                               b'SF_BOOT_V1 synthetic-guest shell_ready historical\n',
                               b'SF_BOOT_V1 synthetic-guest unknown\n'])
def test_complete_challenge_retry_or_invalid_protocol_fails_at_every_split(bad):
    raw = LOGIN+bad
    for split in range(1,len(raw)):
        detector = BootDetector(PROTOCOL)
        got = []
        with pytest.raises(RuntimeError, match='^Synthetic guest (authentication unavailable|console invalid)$'):
            for end in (split,len(raw)):
                for event in detector.observe(raw[:end]): got.append(event)
        assert got == ['login']


def test_unsolicited_shell_record_is_invalid_not_an_authenticated_shortcut():
    with pytest.raises(RuntimeError, match='^Synthetic guest console invalid$'):
        list(BootDetector(PROTOCOL).observe(SHELL))


@pytest.mark.parametrize('hostname', ['', 'Last failed', '-host', 'host\nPassword:', 'a'*64, True])
def test_operator_protocol_configuration_is_exact_and_validated(hostname):
    with pytest.raises(ValueError, match='^Invalid synthetic boot configuration$'):
        BootProtocol(hostname)


def test_boot_detector_rejects_rewound_or_nonbyte_transcripts():
    detector = BootDetector(PROTOCOL)
    assert list(detector.observe(b'booting\n')) == []
    for raw in (b'x', 'not bytes'):
        with pytest.raises(RuntimeError, match='^Synthetic guest console invalid$'):
            list(detector.observe(raw))


def transport(tmp_path, protocol=None, *, lifetime_seconds=120):
    guest = GuestRegistration('g_'+'3'*32, 'synthetic-console', frozenset({Diagnostic.CAPACITY}))
    return SyntheticConsoleTransport(guest, SyntheticVMPolicy(tmp_path/'base', tmp_path/'overlay',
                                                              lifetime_seconds=lifetime_seconds),
                                     boot_protocol=protocol)


def test_missing_protocol_denies_before_preflight_or_launch(tmp_path, monkeypatch):
    import workbench.vm_synthetic as module
    def forbidden(*args, **kwargs):
        pytest.fail('Missing protocol must fail before launch/preflight')
    monkeypatch.setattr(module, 'require_cgroup_bounds', forbidden)
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='^Synthetic guest boot configuration unavailable$'):
        transport(tmp_path).__enter__()


@pytest.mark.parametrize('challenge', [False, True])
def test_bootstrap_uses_complete_events_single_deadline_and_fixed_cleanup(tmp_path, monkeypatch, challenge, capsys):
    import workbench.vm_synthetic as module
    selected = transport(tmp_path, PROTOCOL)
    @contextmanager
    def plan(*args):
        yield SimpleNamespace(argv=(), pass_fds=())
    monkeypatch.setattr(module, 'sandbox_plan', plan)
    monkeypatch.setattr(module, 'require_cgroup_bounds', lambda:None)
    process = SimpleNamespace(poll=lambda:0, stdin=None, stdout=None, stderr=None)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs:process)
    timers = []
    class Timer:
        def __init__(self, seconds, callback):
            self.callback, self.cancelled = callback, False
            timers.append(self)
        def start(self): pass
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(module.threading, 'Timer', Timer)
    writes = []
    monkeypatch.setattr(selected, '_write', lambda payload, deadline:writes.append((payload,deadline)))
    raw = b'Last failed login: \n'+LOGIN+b'root\nPassword: historical text\n'+(AUTH if challenge else SHELL)
    def read(deadline, cap, predicate):
        assert cap == 512*1024
        for end in range(1,len(raw)+1):
            result = predicate(raw[:end])
            if result is not None: return result
        pytest.fail('No complete readiness event')
    monkeypatch.setattr(selected, '_read', read)
    if challenge:
        with pytest.raises(RuntimeError, match='^Synthetic guest boot failed$'):
            selected.__enter__()
    else:
        with selected:
            assert selected._process is process
    assert timers[0].cancelled and timers[0].callback == selected._close_quietly
    assert writes == [(b'root\n',selected._start+selected._policy.lifetime_seconds)]
    assert selected.reaped and capsys.readouterr() == ('','')


def test_timer_inherits_current_thread_daemon_state_without_override():
    timer = threading.Timer(120, lambda:None)
    assert timer.daemon is threading.current_thread().daemon
    assert timer.daemon is False  # main-thread fixture, timer never started


@pytest.mark.parametrize('challenge', [False, True])
def test_real_pipe_bytewise_history_records_and_owned_cleanup(tmp_path, monkeypatch, challenge, capfd):
    import workbench.vm_synthetic as module
    selected = transport(tmp_path, PROTOCOL, lifetime_seconds=2)
    history = b'Last failed login: \nPassword: historical text\nsynthetic-guest login: history\n'
    code = (
        'import os,time\n'
        'def emit(data):\n'
        ' for byte in data:\n'
        '  os.write(1,bytes([byte])); time.sleep(0.001)\n'
        f'emit({history!r}+{LOGIN!r})\n'
        'assert os.read(0,5)==b"root\\n"\n'
        f'emit({history!r}+{(AUTH if challenge else SHELL)!r})\n'
        'time.sleep(10)\n'
    )
    @contextmanager
    def plan(*args):
        yield SimpleNamespace(argv=(sys.executable,'-c',code), pass_fds=())
    monkeypatch.setattr(module, 'sandbox_plan', plan)
    monkeypatch.setattr(module, 'require_cgroup_bounds', lambda:None)
    try:
        if challenge:
            with pytest.raises(RuntimeError, match='^Synthetic guest boot failed$'):
                selected.__enter__()
        else:
            with selected:
                assert selected._process.poll() is None
        assert selected.reaped
        assert all(stream.closed for stream in (selected._process.stdin,selected._process.stdout,selected._process.stderr))
        assert capfd.readouterr().err == ''
    finally:
        selected.close()
