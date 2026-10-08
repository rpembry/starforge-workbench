"""Synthetic console/cgroup contracts without booting a VM in normal pytest."""
import io
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
import time
import subprocess
import sys

import pytest

from workbench.vm_diagnostics import Diagnostic, DiagnosticService, GuestRegistration
from workbench.vm_sandbox import SyntheticVMPolicy
from workbench.vm_synthetic import SyntheticConsoleTransport, framed_result, require_cgroup_bounds


TOKEN = 'a' * 32
GUEST = GuestRegistration('g_' + 'a' * 32, 'synthetic-console', frozenset(Diagnostic))


def bounds(tmp_path, **changes):
    membership = tmp_path/'membership'
    membership.write_text('0::/synthetic\n')
    root = tmp_path/'groups'
    group = root/'synthetic'
    group.mkdir(parents=True)
    values = {'memory.max': '1610612736', 'memory.swap.max': '0',
              'cpu.max': '100000 100000', 'pids.max': '128'}
    values.update(changes)
    for key, value in values.items():
        (group/key).write_text(value)
    return root, membership


def test_cgroup_contract_verifies_hard_bounds_without_writing(tmp_path):
    root, membership = bounds(tmp_path)
    before = {p.name:p.read_bytes() for p in (root/'synthetic').iterdir()}
    require_cgroup_bounds(root, membership)
    assert before == {p.name:p.read_bytes() for p in (root/'synthetic').iterdir()}


@pytest.mark.parametrize('changes', [
    {'memory.max': 'max'}, {'memory.max': '1610612737'}, {'memory.max': '0'},
    {'memory.swap.max': '1'}, {'cpu.max': '200000 100000'}, {'cpu.max': 'max 100000'},
    {'pids.max': '129'}, {'pids.max': 'max'}, {'cpu.max': '0 100000'},
])
def test_missing_or_excessive_resource_limits_fail_closed(tmp_path, changes):
    with pytest.raises(RuntimeError, match='Synthetic VM resource bounds unavailable'):
        require_cgroup_bounds(*bounds(tmp_path, **changes))


def frame(payload, token=TOKEN):
    return ('SF_BEGIN_'+token+'\n').encode()+payload+('\nSF_END_'+token+'\n').encode()


def test_only_complete_whole_marker_lines_are_diagnostic_frames():
    payload = b'{"reachable":true}'
    assert framed_result(frame(payload), TOKEN, 4096) == payload
    echo = ("root@synthetic:~# printf 'SF_BEGIN_"+TOKEN+"\\n'; printf 'SF_END_"+TOKEN+"\\n'\r\n").encode()
    assert framed_result(echo, TOKEN, 4096) is None
    assert framed_result(echo+frame(payload), TOKEN, 4096) == payload
    assert framed_result(b'\x1b[?2004l\r\r\n'+frame(payload), TOKEN, 4096) == payload
    assert framed_result(frame(payload, 'b'*32), TOKEN, 4096) is None
    assert framed_result(frame(payload)[:-10], TOKEN, 4096) is None


def test_frames_enforce_payload_byte_cap_and_token_format():
    assert framed_result(frame(b'x'*4096), TOKEN, 4096) == b'x'*4096
    with pytest.raises(ValueError, match='output cap'):
        framed_result(frame(b'x'*4097), TOKEN, 4096)
    with pytest.raises(ValueError, match='Invalid synthetic diagnostic frame'):
        framed_result(frame(b'x'), '.*', 4096)


def adapter(tmp_path, guest=GUEST):
    return SyntheticConsoleTransport(guest, SyntheticVMPolicy(tmp_path/'base', tmp_path/'overlay'))


@pytest.mark.parametrize('operation,timeout,cap', [
    ('connectivity',5,4096), ('shell',5,4096), (Diagnostic.CONNECTIVITY,6,4096),
    (Diagnostic.CONNECTIVITY,True,4096), (Diagnostic.CONNECTIVITY,5,4097),
    (Diagnostic.CONNECTIVITY,5,True),
])
def test_direct_transport_invocation_cannot_expand_commands_or_bounds(tmp_path, operation, timeout, cap):
    with pytest.raises(RuntimeError, match='not authorized'):
        adapter(tmp_path).run(GUEST, operation, timeout_seconds=timeout, max_output_bytes=cap)


def test_transport_binds_exact_guest_and_operator_authority(tmp_path):
    selected = adapter(tmp_path)
    other = GuestRegistration('g_'+'b'*32, 'synthetic-console', frozenset(Diagnostic))
    with pytest.raises(RuntimeError, match='not authorized'):
        selected.run(other, Diagnostic.CONNECTIVITY, timeout_seconds=5, max_output_bytes=4096)
    denied = GuestRegistration(GUEST.guest_id, 'synthetic-console', frozenset())
    with pytest.raises(RuntimeError, match='not authorized'):
        adapter(tmp_path,denied).run(denied, Diagnostic.CONNECTIVITY, timeout_seconds=5, max_output_bytes=4096)
    with pytest.raises(ValueError, match='Invalid synthetic guest configuration'):
        adapter(tmp_path, GuestRegistration(GUEST.guest_id, 'other-transport', frozenset(Diagnostic)))


def test_stderr_cannot_forge_a_diagnostic_frame_and_counts_toward_cap(tmp_path, monkeypatch):
    import workbench.vm_synthetic as module
    selected = adapter(tmp_path)
    selected._process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda:11),
                                         stderr=SimpleNamespace(fileno=lambda:12), poll=lambda:None)
    monkeypatch.setattr(module.select,'select',lambda *args:([selected._process.stderr,selected._process.stdout],[],[]))
    payload=b'{"reachable":true}'
    monkeypatch.setattr(module.os,'read',lambda fd,n:frame(b'SYNTHETIC_STDERR_SECRET') if fd==12 else frame(payload))
    result=selected._read(time.monotonic()+1,4096,lambda raw:framed_result(raw,TOKEN,4096))
    assert result==payload
    monkeypatch.setattr(module.os,'read',lambda fd,n:b'x'*4097)
    with pytest.raises(RuntimeError,match='output cap exceeded'):
        selected._read(time.monotonic()+1,4096,lambda raw:None)


def test_fixed_console_command_uses_domain_result_release_validation(tmp_path, monkeypatch):
    selected=adapter(tmp_path)
    sink=io.BytesIO()
    selected._process=SimpleNamespace(stdin=sink,poll=lambda:None)
    selected._start=time.monotonic()
    monkeypatch.setattr(selected,'_write',lambda payload,deadline:sink.write(payload))
    monkeypatch.setattr(selected,'_read',lambda *args:b'{"reachable":true}')
    result=DiagnosticService(registrations=(GUEST,),transport=selected).execute(GUEST.guest_id,'connectivity')
    assert result['data']=={'reachable':True}
    assert b'printf' in sink.getvalue() and b'SF_BEGIN_' in sink.getvalue()
    monkeypatch.setattr(selected,'_read',lambda *args:b'{"reachable":true,"secret":"SYNTHETIC_CANARY"}')
    result=DiagnosticService(registrations=(GUEST,),transport=selected).execute(GUEST.guest_id,'connectivity')
    assert result['reason']=='diagnostic_failed' and 'SYNTHETIC_CANARY' not in str(result)


@pytest.mark.parametrize('filled_pipe', [True, False])
def test_stalled_console_obeys_submission_and_collection_deadline(tmp_path, capfd, filled_pipe):
    selected = adapter(tmp_path)
    # Harmless child owns a pipe and never consumes it. Fill the actual kernel
    # pipe rather than mocking select readiness or relying on an external timer.
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
    selected._process = process
    selected._start = time.monotonic()
    try:
        if filled_pipe:
            capacity = fcntl.fcntl(process.stdin.fileno(), fcntl.F_GETPIPE_SZ)
            assert os.write(process.stdin.fileno(), b'x' * capacity) == capacity
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='^Synthetic diagnostic failed$'):
            selected.run(GUEST, Diagnostic.CONNECTIVITY, timeout_seconds=1, max_output_bytes=4096)
        assert time.monotonic() - started < 1.6
        assert selected.reaped
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
        assert capfd.readouterr().err == ''
    finally:
        selected.close()


def test_submission_handles_partial_writes_and_backpressure(tmp_path, monkeypatch):
    import workbench.vm_synthetic as module
    selected = adapter(tmp_path)
    selected._process = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda:11), poll=lambda:None)
    received = bytearray()
    attempts = 0
    def write(fd, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise BlockingIOError
        received.extend(payload[:3])
        return min(3, len(payload))
    monkeypatch.setattr(module.os, 'set_blocking', lambda fd, value:None)
    monkeypatch.setattr(module.os, 'write', write)
    monkeypatch.setattr(module.select, 'select', lambda *args:([], [11], []))
    selected._write(b'fixed command\n', time.monotonic()+1)
    assert received == b'fixed command\n' and attempts > 2
    with pytest.raises(RuntimeError, match='^Synthetic console unavailable$'):
        selected._write(b'next', time.monotonic()-1)


def test_cleanup_closes_every_stream_despite_broken_pipe(tmp_path, capfd):
    import threading
    selected = adapter(tmp_path)
    closed = []
    class BrokenStream:
        def close(self):
            closed.append(self)
            raise BrokenPipeError('SYNTHETIC_PRIVATE_PATH')
    streams = [BrokenStream() for _ in range(3)]
    selected._process = SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2],
                                        poll=lambda:0)
    selected.close()
    assert closed == streams
    closed.clear()
    timer = threading.Timer(0, selected._close_quietly)
    timer.start()
    timer.join(1)
    assert not timer.is_alive() and closed == streams
    assert capfd.readouterr().err == ''


def test_cleanup_failure_is_fixed_and_does_not_replace_diagnostic_error(tmp_path, monkeypatch, capfd):
    import workbench.vm_synthetic as module
    selected = adapter(tmp_path)
    selected._start = time.monotonic()
    closed = []
    streams = [SimpleNamespace(close=lambda:closed.append(True)) for _ in range(3)]
    def wait(**kwargs):
        raise OSError('SYNTHETIC_PRIVATE_PATH')
    selected._process = SimpleNamespace(pid=123, stdin=streams[0], stdout=streams[1],
                                        stderr=streams[2], poll=lambda:None, wait=wait)
    monkeypatch.setattr(module.os, 'killpg', lambda *args:None)
    with pytest.raises(RuntimeError, match='^Synthetic guest cleanup failed$'):
        selected.close()
    assert len(closed) == 3
    def broken_write(*args):
        raise BrokenPipeError('SYNTHETIC_PRIVATE_PATH')
    monkeypatch.setattr(selected, '_write', broken_write)
    with pytest.raises(RuntimeError, match='^Synthetic diagnostic failed$'):
        selected.run(GUEST, Diagnostic.CONNECTIVITY, timeout_seconds=1, max_output_bytes=4096)
    selected._close_quietly()
    assert capfd.readouterr().err == ''
