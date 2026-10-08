"""Opt-in disposable synthetic console adapter; never a general VM transport.

Operator creates a private verified base/overlay and a bounded transient user
cgroup before entering this adapter. No host registration/auth is installed.
"""
import os
from dataclasses import dataclass
from pathlib import Path
import re
import resource
import select
import signal
import subprocess
import threading
import time
import uuid

from .vm_diagnostics import Diagnostic, GuestRegistration, MAX_OUTPUT_BYTES
from .vm_sandbox import SyntheticVMPolicy, diagnostic_script, sandbox_plan


def require_cgroup_bounds(root=Path('/sys/fs/cgroup'), membership=Path('/proc/self/cgroup')):
    """Verify active v2 bounds; no controller/settings writes are performed."""
    try:
        location = next(line.split(':', 2)[2].strip().lstrip('/')
                        for line in membership.read_text().splitlines() if line.startswith('0:'))
        if '..' in Path(location).parts:
            raise ValueError
        group = root / location
        memory = int((group / 'memory.max').read_text())
        swap = int((group / 'memory.swap.max').read_text())
        tasks = int((group / 'pids.max').read_text())
        quota, period = map(int, (group / 'cpu.max').read_text().split())
        if not (0 < memory <= 1536 * 1024**2 and swap == 0 and 0 < tasks <= 128
                and 0 < quota <= period):
            raise ValueError
    except (OSError, ValueError, StopIteration):
        raise RuntimeError('Synthetic VM resource bounds unavailable') from None


def _process_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def framed_result(raw: bytes, token: str, maximum: int) -> bytes | None:
    """Match whole marker lines; echoed shell text is never a result frame."""
    if type(raw) is not bytes or type(token) is not str or not re.fullmatch('[a-f0-9]{32}', token):
        raise ValueError('Invalid synthetic diagnostic frame')
    begin = ('SF_BEGIN_' + token).encode()
    end = ('SF_END_' + token).encode()
    match = re.search(rb'(?:^|\n)' + begin + rb'\r?\n(.*?)\n' + end + rb'\r?(?:\n|$)',
                      raw, re.S)
    if match is None:
        return None
    result = match.group(1).strip()
    if len(result) > maximum:
        raise ValueError('Synthetic diagnostic output cap exceeded')
    return result


@dataclass(frozen=True, slots=True)
class BootPrompts:
    """Operator-selected grammar for the controlled image, never autodetected."""
    hostname: str

    def __post_init__(self):
        if type(self.hostname) is not str or not re.fullmatch('[a-z][a-z0-9-]{0,62}', self.hostname):
            raise ValueError('Invalid synthetic boot configuration')


class BootDetector:
    """Current-line state machine over append-only, bounded console bytes.

    This recognizes prompt syntax, not authenticated guest identity. Exact
    prompt impersonation by a malicious guest remains outside this fixture.
    """
    def __init__(self, prompts):
        if type(prompts) is not BootPrompts:
            raise ValueError('Invalid synthetic boot configuration')
        host = prompts.hostname.encode('ascii')
        self._login = host + b' login: '
        self._shell = b'root@' + host + b':~# '
        self._state = 'login'
        self._length = 0

    def observe(self, raw):
        if type(raw) is not bytes or len(raw) < self._length:
            raise RuntimeError('Synthetic guest console invalid')
        if self._state == 'ready':
            return 'ready'
        if len(raw) == self._length:
            return None
        self._length = len(raw)
        # CR and LF both delimit the current terminal line. Only one known
        # bracketed-paste enable prefix is accepted for the root shell prompt;
        # arbitrary ANSI rewriting/control sequences are not interpreted.
        line = raw.rsplit(b'\n', 1)[-1].rsplit(b'\r', 1)[-1]
        if self._state == 'login':
            if line == self._login:
                self._state = 'shell'
                return 'login'
            return None
        if line in (self._shell, b'\x1b[?2004h' + self._shell):
            self._state = 'ready'
            return 'ready'
        if line == b'Password: ' or line == self._login:
            raise RuntimeError('Synthetic guest authentication unavailable')
        return None


class SyntheticConsoleTransport:
    """Fixed diagnostics for one freshly created operator-owned test guest.

    No raw serial bytes are logged, stored, or returned by boot/error paths.
    Rejected execution/collection terminates the guest; caller must create a
    fresh overlay before another attempt. It is deliberately unsuitable for
    existing/private guests and supplies no live registration to wb-vm-mcp.
    """
    def __init__(self, guest: GuestRegistration, policy: SyntheticVMPolicy, *, prompts=None):
        if (type(guest) is not GuestRegistration or guest.transport_ref != 'synthetic-console'
                or type(policy) is not SyntheticVMPolicy
                or (prompts is not None and type(prompts) is not BootPrompts)):
            raise ValueError('Invalid synthetic guest configuration')
        self._guest = guest
        self._policy = policy
        self._prompts = prompts
        self._process = None
        self._timer = None
        self._start = None
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()

    @property
    def reaped(self):
        return self._process is not None and self._process.poll() is not None

    def _write(self, payload, deadline):
        """Submit through the pipe without buffered writes or an unbounded flush."""
        fd = self._process.stdin.fileno()
        os.set_blocking(fd, False)
        remaining = memoryview(payload)
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0 or self._process.poll() is not None:
                raise RuntimeError('Synthetic console unavailable')
            _, writable, _ = select.select([], [fd], [], min(0.25, budget))
            if not writable:
                continue
            try:
                written = os.write(fd, remaining)
            except BlockingIOError:
                continue
            if written <= 0:
                raise RuntimeError('Synthetic console unavailable')
            remaining = remaining[written:]

    def _read(self, deadline, maximum, predicate):
        collected = bytearray()
        total_bytes = 0
        while time.monotonic() < deadline and self._process.poll() is None:
            ready, _, _ = select.select([self._process.stdout, self._process.stderr], [], [],
                                        min(0.25, max(0, deadline - time.monotonic())))
            for stream in ready:
                chunk = os.read(stream.fileno(), min(4096, maximum - total_bytes + 1))
                if not chunk:
                    continue
                # stderr bytes count toward the cap but are never parsed as stdout.
                total_bytes += len(chunk)
                if stream is self._process.stdout:
                    collected.extend(chunk)
                if total_bytes > maximum:
                    raise RuntimeError('Synthetic console output cap exceeded')
            result = predicate(bytes(collected))
            if result is not None:
                return result
        raise RuntimeError('Synthetic console unavailable')

    def __enter__(self):
        if self._process is not None:
            raise RuntimeError('Synthetic guest already used')
        if self._prompts is None:
            raise RuntimeError('Synthetic guest boot configuration unavailable')
        require_cgroup_bounds()
        self._start = time.monotonic()
        try:
            with sandbox_plan(self._policy) as plan:
                self._process = subprocess.Popen(plan.argv, pass_fds=plan.pass_fds, close_fds=True,
                                                env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, start_new_session=True,
                                                preexec_fn=_process_limits, bufsize=0)
            self._timer = threading.Timer(self._policy.lifetime_seconds, self._close_quietly)
            self._timer.start()
            detector = BootDetector(self._prompts)
            def boot(raw):
                event = detector.observe(raw)
                if event == 'login':
                    self._write(b'root\n', self._start + self._policy.lifetime_seconds)
                if event == 'ready':
                    return True
                return None
            self._read(self._start + self._policy.lifetime_seconds, 512 * 1024, boot)
            return self
        except Exception:
            self._close_quietly()
            raise RuntimeError('Synthetic guest boot failed') from None

    def run(self, guest, operation, *, timeout_seconds, max_output_bytes):
        if (guest != self._guest or type(operation) is not Diagnostic
                or operation not in self._guest.allowed
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 5
                or type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES):
            raise RuntimeError('Synthetic diagnostic not authorized')
        deadline = time.monotonic() + timeout_seconds
        if not self._lock.acquire(timeout=timeout_seconds):
            raise RuntimeError('Synthetic diagnostic unavailable')
        try:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError('Synthetic guest unavailable')
            token = uuid.uuid4().hex
            command = ("stty -echo; printf '\\nSF_BEGIN_" + token + "\\n'; "
                       + diagnostic_script(operation.value)
                       + "; printf 'SF_END_" + token + "\\n'\n")
            self._write(command.encode(), min(deadline, self._start + self._policy.lifetime_seconds))
            # Framing and the first echoed fixed command need a small fixed
            # overhead; payload itself is independently capped at requested size.
            return self._read(min(deadline, self._start + self._policy.lifetime_seconds),
                              max_output_bytes + 2048,
                              lambda raw: framed_result(raw, token, max_output_bytes))
        except Exception:
            self._close_quietly()
            raise RuntimeError('Synthetic diagnostic failed') from None
        finally:
            self._lock.release()

    def close(self):
        with self._cleanup_lock:
            self._close()

    def _close_quietly(self):
        # Timer and failure paths must never emit a traceback or replace the
        # fixed diagnostic error. Reaping remains observable via `reaped`.
        try:
            self.close()
        except Exception:
            pass

    def _close(self):
        if self._timer is not None:
            self._timer.cancel()
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if process.poll() is None:
                raise RuntimeError('Synthetic guest cleanup failed')
        except Exception:
            raise RuntimeError('Synthetic guest cleanup failed') from None
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        # A close failure must not prevent other streams from
                        # closing or disclose raw exception text to stderr.
                        pass

    def __exit__(self, *args):
        self.close()
