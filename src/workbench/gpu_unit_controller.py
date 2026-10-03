"""Exact user-unit ownership boundary for the opt-in GPU reservation adapter.

This module does not install or start a service. An operator must supply one
reviewed user unit name and a workload-scope probe before instantiating it.
"""

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Protocol

from .gpu_policy import Observation


class EvidenceUnavailable(RuntimeError):
    """Required ownership or GPU evidence could not be established."""


@dataclass(frozen=True)
class ProcessGeneration:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class UnitGeneration:
    unit: str
    state: str
    invocation: str
    cgroup: str
    main_pid: int
    processes: tuple[ProcessGeneration, ...]
    start_job: bool | None = None  # None means job visibility is unavailable.
    cgroup_inode: int = 0


class UserUnit(Protocol):
    unit: str

    def snapshot(self) -> UnitGeneration: ...
    def cgroup_processes(self, cgroup: str) -> tuple[ProcessGeneration, ...] | None: ...
    def cgroup_inode(self, cgroup: str) -> int | None: ...
    def generation_alive(self, process: ProcessGeneration) -> bool | None: ...
    def stop(self, expected: UnitGeneration) -> None: ...
    def start(self) -> None: ...


class GpuProcessProbe(Protocol):
    def pids(self) -> set[int] | None: ...


class WorkloadScopeProbe(Protocol):
    def ended(self, owner: str) -> bool | None: ...
    def permitted_gpu_pids(self, active_leases) -> set[int] | None: ...


class UnitReceiptSource(Protocol):
    def latest_capture(self, unit: str) -> UnitGeneration | None: ...
    def record_capture(self, captured: UnitGeneration) -> None: ...
    def completed(self, unit: str, invocation: str, *,
                  captured: UnitGeneration | None = None) -> bool: ...


class LibcPidfd:
    """Use libc's pidfd entry points when Python lacks pidfd helpers."""

    def __init__(self):
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            self._open = libc.pidfd_open
            self._send = libc.pidfd_send_signal
        except AttributeError:
            raise EvidenceUnavailable("Exact process signaling is unavailable") from None
        self._open.argtypes = (ctypes.c_int, ctypes.c_uint)
        self._open.restype = ctypes.c_int
        self._send.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                               ctypes.c_uint)
        self._send.restype = ctypes.c_int

    def open(self, pid: int) -> int:
        fd = self._open(pid, 0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "pidfd_open failed")
        return fd

    def send(self, fd: int, signum: int) -> None:
        if self._send(fd, signum, None, 0) < 0:
            raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


class ExactUnitController:
    """Control only one configured unit; grant from positive exit evidence.

    A production integration supplies a durable receipt source. It captures
    the process/cgroup generation before stop and requires a matching systemd
    completion receipt, even when inactive systemd clears InvocationID.
    """

    def __init__(self, unit: UserUnit, gpu: GpuProcessProbe,
                 scopes: WorkloadScopeProbe, receipts: UnitReceiptSource | None = None):
        self.unit = unit
        self.gpu = gpu
        self.scopes = scopes
        self.receipts = receipts
        try:
            self._stopped_generation = (receipts.latest_capture(unit.unit)
                                        if receipts is not None else None)
        except Exception:
            self._stopped_generation = None

    def observe(self, active_leases=()) -> tuple[Observation, Observation, bool]:
        try:
            current = self.unit.snapshot()
        except EvidenceUnavailable:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if current.unit != self.unit.unit:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if current.state == 'active':
            return Observation.PRESENT, Observation.UNKNOWN, True
        if (current.state != 'inactive' or current.start_job is not False or
                self._stopped_generation is None):
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        captured = self._stopped_generation
        if current.invocation not in ('', captured.invocation):
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if self.receipts is None:
            if current.invocation != captured.invocation:
                return Observation.UNKNOWN, Observation.UNKNOWN, True
        else:
            try:
                if not self.receipts.completed(captured.unit, captured.invocation,
                                               captured=captured):
                    return Observation.UNKNOWN, Observation.UNKNOWN, True
            except Exception:
                return Observation.UNKNOWN, Observation.UNKNOWN, True
        try:
            inode = self.unit.cgroup_inode(captured.cgroup)
        except Exception:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if inode is None or (captured.cgroup_inode > 0 and
                             inode not in (0, captured.cgroup_inode)):
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        remaining = self.unit.cgroup_processes(captured.cgroup)
        if remaining is None:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if remaining:
            return Observation.PRESENT, Observation.UNKNOWN, True
        for process in captured.processes:
            alive = self.unit.generation_alive(process)
            if alive is None:
                return Observation.UNKNOWN, Observation.UNKNOWN, True
            if alive:
                return Observation.PRESENT, Observation.UNKNOWN, True
        gpu_pids = self.gpu.pids()
        if gpu_pids is None:
            return Observation.ABSENT, Observation.UNKNOWN, True
        if gpu_pids.intersection(p.pid for p in captured.processes):
            return Observation.ABSENT, Observation.PRESENT, True
        permitted = self.scopes.permitted_gpu_pids(active_leases)
        if permitted is None or not gpu_pids.issubset(permitted):
            # A late escaped miner child and an unrelated GPU client are
            # indistinguishable without positive workload-scope attribution.
            return Observation.ABSENT, Observation.UNKNOWN, True
        if self.gpu.pids() != gpu_pids or self.scopes.permitted_gpu_pids(active_leases) != permitted:
            return Observation.ABSENT, Observation.UNKNOWN, True
        try:
            final = self.unit.snapshot()
        except EvidenceUnavailable:
            return Observation.ABSENT, Observation.UNKNOWN, True
        if (final.unit != current.unit or final.state != 'inactive' or
                final.start_job is not False or
                final.invocation not in ('', captured.invocation)):
            return Observation.ABSENT, Observation.UNKNOWN, True
        if self.receipts is None:
            if final.invocation != captured.invocation:
                return Observation.ABSENT, Observation.UNKNOWN, True
        else:
            try:
                if not self.receipts.completed(captured.unit, captured.invocation,
                                               captured=captured):
                    return Observation.ABSENT, Observation.UNKNOWN, True
            except Exception:
                return Observation.ABSENT, Observation.UNKNOWN, True
        # The unit's existing watcher remains the idle authority after START.
        return Observation.ABSENT, Observation.ABSENT, True

    def workload_ended(self, owner: str) -> bool | None:
        return self.scopes.ended(owner)

    def apply(self, action: str) -> None:
        if action == 'STOP':
            current = self.unit.snapshot()
            if (current.unit == self.unit.unit and current.state == 'inactive' and
                    self._stopped_generation is not None):
                # Never signal a guessed escaped child or another service.
                return
            if (current.unit != self.unit.unit or current.state != 'active' or
                    not current.invocation or not current.cgroup or
                    not current.processes or
                    current.main_pid not in {p.pid for p in current.processes}):
                raise EvidenceUnavailable("Cannot identify exact active unit generation")
            if self.receipts is not None:
                try:
                    self.receipts.record_capture(current)
                except Exception:
                    raise EvidenceUnavailable('Cannot persist exact stop capture') from None
            self._stopped_generation = current
            self.unit.stop(current)
        elif action == 'START':
            process, context, _ = self.observe()
            if process is not Observation.ABSENT or context is not Observation.ABSENT:
                raise EvidenceUnavailable("Unit exit is not verified")
            self.unit.start()
            self._stopped_generation = None
        else:
            raise ValueError("Only exact-unit START or STOP is supported")


class SystemdUserUnit:
    """Fixed-argv user-systemd backend for one privately configured unit."""

    def __init__(self, unit: str, *, cgroup_root: Path = Path('/sys/fs/cgroup'),
                 pidfd=None):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit):
            raise ValueError("An exact user service name is required")
        self.unit = unit
        self.cgroup_root = cgroup_root
        self.pidfd = pidfd

    @staticmethod
    def _run(*args: str) -> str:
        try:
            result = subprocess.run(
                ('systemctl', '--user', *args), capture_output=True, text=True,
                timeout=15, check=True)
        except (OSError, subprocess.SubprocessError):
            raise EvidenceUnavailable("User unit operation unavailable") from None
        return result.stdout

    @staticmethod
    def _start_ticks(pid: int) -> int | None:
        try:
            data = Path(f'/proc/{pid}/stat').read_text()
            return int(data.rsplit(')', 1)[1].split()[19])
        except FileNotFoundError:
            return None
        except (OSError, IndexError, ValueError):
            raise EvidenceUnavailable("Process generation unavailable") from None

    def cgroup_processes(self, cgroup: str) -> tuple[ProcessGeneration, ...] | None:
        if (not cgroup.startswith('/user.slice/') or '..' in cgroup.split('/') or
                not cgroup.endswith('/' + self.unit)):
            return None
        directory = self.cgroup_root / cgroup.lstrip('/')
        try:
            directory.stat()
            files = list(directory.rglob('cgroup.procs'))
            if not files or len(files) > 1024:
                return None
            contents = ' '.join(path.read_text() for path in files)
        except FileNotFoundError:
            return ()
        except OSError:
            return None
        try:
            pids = {int(value) for value in contents.split()}
            if len(pids) > 4096 or any(pid <= 0 for pid in pids):
                return None
            processes = []
            for pid in sorted(pids):
                start = self._start_ticks(pid)
                if start is None:
                    return None  # A sampling race needs another observation.
                processes.append(ProcessGeneration(pid, start))
            return tuple(processes)
        except (ValueError, EvidenceUnavailable):
            return None

    def cgroup_inode(self, cgroup: str) -> int | None:
        if (not cgroup.startswith('/user.slice/') or '..' in cgroup.split('/') or
                not cgroup.endswith('/' + self.unit)):
            return None
        try:
            return (self.cgroup_root / cgroup.lstrip('/')).stat().st_ino
        except FileNotFoundError:
            return 0
        except OSError:
            return None

    def generation_alive(self, process: ProcessGeneration) -> bool | None:
        try:
            start = self._start_ticks(process.pid)
        except EvidenceUnavailable:
            return None
        return start == process.start_ticks if start is not None else False

    def snapshot(self) -> UnitGeneration:
        output = self._run('show', self.unit, '--no-pager', '-p', 'ActiveState',
                           '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'InvocationID',
                           '-p', 'Job')
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if set(fields) != {'ActiveState', 'MainPID', 'ControlGroup', 'InvocationID', 'Job'}:
            raise EvidenceUnavailable("Incomplete user unit properties")
        try:
            main_pid = int(fields['MainPID'])
        except ValueError:
            raise EvidenceUnavailable("Invalid main PID") from None
        state = fields['ActiveState']
        job = fields['Job']
        # systemctl show prints Job= (empty) when no job exists on this host.
        # An omitted property is rejected above; a malformed value remains
        # unknown rather than being mistaken for the observed empty form.
        if job in ('', '0'):
            start_job = False
        elif re.fullmatch(r'[1-9][0-9]*(?:/.*)?', job):
            start_job = True
        else:
            start_job = None
        cgroup = fields['ControlGroup']
        processes = self.cgroup_processes(cgroup) if state == 'active' else ()
        inode = self.cgroup_inode(cgroup) if state == 'active' else 0
        if state == 'active' and (processes is None or main_pid <= 0 or
                                  main_pid not in {p.pid for p in processes} or
                                  inode is None or inode <= 0):
            raise EvidenceUnavailable("Active unit generation is incomplete")
        return UnitGeneration(self.unit, state, fields['InvocationID'], cgroup,
                              main_pid, processes, start_job, inode)

    def stop(self, expected: UnitGeneration) -> None:
        """Signal the captured main process, never a replacement unit name.

        The unit's own watcher must stop its children on TERM. If it does not,
        subsequent process/cgroup/GPU checks remain pending; no broad fallback
        signal is sent here.
        """
        current = self.snapshot()
        if (current.unit != expected.unit or current.invocation != expected.invocation or
                current.main_pid != expected.main_pid or
                current.cgroup_inode != expected.cgroup_inode):
            raise EvidenceUnavailable("Unit invocation changed before exact stop")
        main = next((p for p in expected.processes if p.pid == expected.main_pid), None)
        if main is None:
            raise EvidenceUnavailable("Captured main process is missing")
        ops = self.pidfd or LibcPidfd()
        try:
            fd = ops.open(main.pid)
        except OSError:
            raise EvidenceUnavailable("Cannot bind captured main process") from None
        try:
            if self._start_ticks(main.pid) != main.start_ticks:
                raise EvidenceUnavailable("Main process generation changed")
            bound = self.snapshot()
            if (bound.unit != expected.unit or bound.invocation != expected.invocation or
                    bound.main_pid != expected.main_pid or
                    bound.cgroup_inode != expected.cgroup_inode):
                raise EvidenceUnavailable("Unit invocation changed after process binding")
            ops.send(fd, signal.SIGTERM)
        except OSError:
            raise EvidenceUnavailable("Exact main process stop unavailable") from None
        finally:
            os.close(fd)

    def start(self) -> None:
        self._run('start', self.unit)


class RocmKfdProcesses:
    """Parse only KFD PIDs; unrelated GPU use does not prevent a grant."""

    def pids(self) -> set[int] | None:
        try:
            result = subprocess.run(('rocm-smi', '--showpids'), capture_output=True,
                                    text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        output = result.stdout
        marker = 'KFD process information:'
        if marker not in output:
            return None
        section = output.split(marker, 1)[1].split('====', 1)[0]
        if 'PID' not in section:
            return None
        pids = set()
        for line in section.splitlines():
            if re.match(r'^\s*\d+\s+', line):
                pids.add(int(line.split()[0]))
        return pids
