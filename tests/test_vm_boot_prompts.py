"""Controlled synthetic boot transcripts; no VM/process/image operations."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from workbench.vm_diagnostics import Diagnostic, GuestRegistration
from workbench.vm_sandbox import SyntheticVMPolicy
from workbench.vm_synthetic import BootDetector, BootPrompts, SyntheticConsoleTransport


PROMPTS = BootPrompts('synthetic-guest')
LOGIN = b'synthetic-guest login: '
SHELL = b'root@synthetic-guest:~# '


@pytest.mark.parametrize('lookalike', [
    b'Last failed login: ', b'Last login: ', b'log: synthetic-guest login: ',
    b'other-host login: ', b'synthetic-guest login:', b'synthetic-guest login: \n',
    b'\x1b[31msynthetic-guest login: ', b'Password: ', SHELL,
    b'synthetic-guest login: root\n', b'prefix synthetic-guest login: ',
])
def test_only_exact_current_controlled_getty_prompt_requests_login(lookalike):
    detector = BootDetector(PROMPTS)
    assert detector.observe(lookalike) is None


def test_history_password_and_login_text_cannot_override_current_shell():
    detector = BootDetector(PROMPTS)
    history = b'Last failed login: \r\nPassword: \r\nMOTD: Password: synthetic historical\r\n'
    raw = history + LOGIN
    assert detector.observe(raw) == 'login'
    # Polling the same accumulated buffer must not classify a retry.
    assert detector.observe(raw) is None
    raw += b'root\r\nPassword: historical example\r\n' + SHELL
    assert detector.observe(raw) == 'ready'


@pytest.mark.parametrize('prefix', [b'', b'\x1b[?2004h'])
def test_every_split_point_of_login_and_shell_transitions(prefix):
    for split in range(1, len(LOGIN)):
        detector = BootDetector(PROMPTS)
        assert detector.observe(LOGIN[:split]) is None
        assert detector.observe(LOGIN) == 'login'
        base = LOGIN + b'root\r\nMOTD\r\n'
        shell = prefix + SHELL
        for offset in range(1, len(shell)):
            other = BootDetector(PROMPTS)
            assert other.observe(LOGIN) == 'login'
            assert other.observe(base+shell[:offset]) is None
            assert other.observe(base+shell) == 'ready'


@pytest.mark.parametrize('challenge', [b'Password: ', LOGIN])
def test_new_current_password_or_repeated_getty_prompt_fails_fixed(challenge):
    detector = BootDetector(PROMPTS)
    assert detector.observe(LOGIN) == 'login'
    base = LOGIN + b'root\r\n'
    assert detector.observe(base+challenge[:-1]) is None
    with pytest.raises(RuntimeError, match='^Synthetic guest authentication unavailable$'):
        detector.observe(base+challenge)


@pytest.mark.parametrize('line', [b'root@other-host:~# ', b'root@synthetic-guest:/tmp# ',
                                 b'log '+SHELL, SHELL+b'\n', b'Password: historical text',
                                 b'Password:\r\nMOTD\r\n', b'\x1b[31m'+SHELL])
def test_shell_state_ignores_history_wrong_identity_and_terminal_rewriting(line):
    detector = BootDetector(PROMPTS)
    assert detector.observe(LOGIN) == 'login'
    assert detector.observe(LOGIN+b'root\r\n'+line) is None


@pytest.mark.parametrize('hostname', ['', 'Last failed', '-host', 'host\nPassword:', 'a'*64, True])
def test_operator_prompt_configuration_is_exact_and_validated(hostname):
    with pytest.raises(ValueError, match='^Invalid synthetic boot configuration$'):
        BootPrompts(hostname)


def test_boot_detector_rejects_rewound_or_nonbyte_transcripts():
    detector = BootDetector(PROMPTS)
    assert detector.observe(b'booting\n') is None
    for raw in (b'x', 'not bytes'):
        with pytest.raises(RuntimeError, match='^Synthetic guest console invalid$'):
            detector.observe(raw)


def transport(tmp_path, prompts=None):
    guest = GuestRegistration('g_'+'3'*32, 'synthetic-console', frozenset({Diagnostic.CAPACITY}))
    return SyntheticConsoleTransport(guest, SyntheticVMPolicy(tmp_path/'base', tmp_path/'overlay'),
                                     prompts=prompts)


def test_missing_boot_grammar_denies_before_preflight_or_launch(tmp_path, monkeypatch):
    import workbench.vm_synthetic as module
    def forbidden(*args, **kwargs):
        pytest.fail('Missing grammar must fail before launch/preflight')
    monkeypatch.setattr(module, 'require_cgroup_bounds', forbidden)
    monkeypatch.setattr(module.subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='^Synthetic guest boot configuration unavailable$'):
        transport(tmp_path).__enter__()


@pytest.mark.parametrize('password_challenge', [False, True])
def test_bootstrap_uses_detector_single_deadline_and_fixed_cleanup(tmp_path, monkeypatch, password_challenge, capsys):
    import workbench.vm_synthetic as module
    selected = transport(tmp_path, PROMPTS)
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
            self.seconds, self.callback = seconds, callback
            self.cancelled = False
            timers.append(self)
        def start(self): pass
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(module.threading, 'Timer', Timer)
    writes = []
    monkeypatch.setattr(selected, '_write', lambda payload, deadline:writes.append((payload, deadline)))
    def read(deadline, cap, predicate):
        assert cap == 512*1024
        assert predicate(b'Last failed login: ') is None
        raw = b'Last failed login: \nPassword: historical\n' + LOGIN
        assert predicate(raw) is None
        assert predicate(raw) is None
        current = b'Password: ' if password_challenge else b'\x1b[?2004h'+SHELL
        assert predicate(raw+b'root\r\n'+current) is True
        return True
    monkeypatch.setattr(selected, '_read', read)
    if password_challenge:
        with pytest.raises(RuntimeError, match='^Synthetic guest boot failed$'):
            selected.__enter__()
        assert timers[0].cancelled
    else:
        with selected:
            assert selected._process is process
        assert timers[0].cancelled
    assert writes == [(b'root\n', selected._start+selected._policy.lifetime_seconds)]
    assert timers[0].callback == selected._close_quietly and selected.reaped
    assert capsys.readouterr() == ('', '')


def test_timer_inherits_current_thread_daemon_state_without_explicit_override():
    timer = threading.Timer(120, lambda:None)
    assert timer.daemon is threading.current_thread().daemon
    assert timer.daemon is False  # pytest main-thread fixture, timer never started
