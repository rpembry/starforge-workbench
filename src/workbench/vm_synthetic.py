"""Opt-in disposable synthetic console adapter; never a general VM transport.

Operator creates a private verified base/overlay and a bounded transient user
cgroup before entering this adapter. No host registration/auth is installed.
"""
import os
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


class SyntheticConsoleTransport:
    """Fixed diagnostics for one freshly created operator-owned test guest.

    No raw serial bytes are logged, stored, or returned by boot/error paths.
    Rejected execution/collection terminates the guest; caller must create a
    fresh overlay before another attempt. It is deliberately unsuitable for
    existing/private guests and supplies no live registration to wb-vm-mcp.
    """
    def __init__(self, guest: GuestRegistration, policy: SyntheticVMPolicy):
        if (type(guest) is not GuestRegistration or guest.transport_ref != 'synthetic-console'
                or type(policy) is not SyntheticVMPolicy):
            raise ValueError('Invalid synthetic guest configuration')
        self._guest = guest
        self._policy = policy
        self._process = None
        self._timer = None
        self._start = None
        self._lock = threading.Lock()
        self._cleanup_lock = threading.Lock()

    @property
    def reaped(self):
        return self._process is not None and self._process.poll() is not None

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
        require_cgroup_bounds()
        self._start = time.monotonic()
        try:
            with sandbox_plan(self._policy) as plan:
                self._process = subprocess.Popen(plan.argv, pass_fds=plan.pass_fds, close_fds=True,
                                                env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, start_new_session=True,
                                                preexec_fn=_process_limits)
            self._timer = threading.Timer(self._policy.lifetime_seconds, self.close)
            self._timer.start()
            sent_login = False
            def boot(raw):
                nonlocal sent_login
                if b'login:' in raw and not sent_login:
                    self._process.stdin.write(b'root\n')
                    self._process.stdin.flush()
                    sent_login = True
                if sent_login and b'Password:' in raw:
                    raise RuntimeError('Synthetic guest authentication unavailable')
                if re.search(rb'root@[^:\r\n]+:[^\r\n]*#\s*$', raw):
                    return True
                return None
            self._read(self._start + self._policy.lifetime_seconds, 512 * 1024, boot)
            return self
        except Exception:
            self.close()
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
            self._process.stdin.write(command.encode())
            self._process.stdin.flush()
            # Framing and the first echoed fixed command need a small fixed
            # overhead; payload itself is independently capped at requested size.
            return self._read(min(deadline, self._start + self._policy.lifetime_seconds),
                              max_output_bytes + 2048,
                              lambda raw: framed_result(raw, token, max_output_bytes))
        except Exception:
            self.close()
            raise RuntimeError('Synthetic diagnostic failed') from None
        finally:
            self._lock.release()

    def close(self):
        with self._cleanup_lock:
            self._close()

    def _close(self):
        if self._timer is not None:
            self._timer.cancel()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def __exit__(self, *args):
        self.close()
