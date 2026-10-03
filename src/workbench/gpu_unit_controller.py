"""Exact user-unit ownership boundary for the opt-in GPU reservation adapter.

This module does not install or start a service. An operator must supply one
reviewed user unit name and a workload-scope probe before instantiating it.
"""

from dataclasses import dataclass
from pathlib import Path
import re
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


class UserUnit(Protocol):
    unit: str

    def snapshot(self) -> UnitGeneration: ...
    def cgroup_processes(self, cgroup: str) -> tuple[ProcessGeneration, ...] | None: ...
    def generation_alive(self, process: ProcessGeneration) -> bool | None: ...
    def stop(self) -> None: ...
    def start(self) -> None: ...


class GpuProcessProbe(Protocol):
    def pids(self) -> set[int] | None: ...


class WorkloadScopeProbe(Protocol):
    def ended(self, owner: str) -> bool | None: ...


class ExactUnitController:
    """Control only one configured unit; grant from positive exit evidence.

    A process restart loses the in-memory captured generation. If the unit is
    already inactive, that is unknown rather than a reason to grant or start.
    A production integration needs reviewed recovery of this capture before
    unattended restart after an adapter crash.
    """

    def __init__(self, unit: UserUnit, gpu: GpuProcessProbe,
                 scopes: WorkloadScopeProbe):
        self.unit = unit
        self.gpu = gpu
        self.scopes = scopes
        self._stopped_generation: UnitGeneration | None = None

    def observe(self) -> tuple[Observation, Observation, bool]:
        try:
            current = self.unit.snapshot()
        except EvidenceUnavailable:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if current.unit != self.unit.unit:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        if current.state == 'active':
            return Observation.PRESENT, Observation.UNKNOWN, True
        if current.state != 'inactive' or self._stopped_generation is None:
            return Observation.UNKNOWN, Observation.UNKNOWN, True
        captured = self._stopped_generation
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
            self._stopped_generation = current
            self.unit.stop()
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

    def __init__(self, unit: str, *, cgroup_root: Path = Path('/sys/fs/cgroup')):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit):
            raise ValueError("An exact user service name is required")
        self.unit = unit
        self.cgroup_root = cgroup_root

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

    def generation_alive(self, process: ProcessGeneration) -> bool | None:
        try:
            start = self._start_ticks(process.pid)
        except EvidenceUnavailable:
            return None
        return start == process.start_ticks if start is not None else False

    def snapshot(self) -> UnitGeneration:
        output = self._run('show', self.unit, '--no-pager', '-p', 'ActiveState',
                           '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'InvocationID')
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if set(fields) != {'ActiveState', 'MainPID', 'ControlGroup', 'InvocationID'}:
            raise EvidenceUnavailable("Incomplete user unit properties")
        try:
            main_pid = int(fields['MainPID'])
        except ValueError:
            raise EvidenceUnavailable("Invalid main PID") from None
        state = fields['ActiveState']
        cgroup = fields['ControlGroup']
        processes = self.cgroup_processes(cgroup) if state == 'active' else ()
        if state == 'active' and (processes is None or main_pid <= 0 or
                                  main_pid not in {p.pid for p in processes}):
            raise EvidenceUnavailable("Active unit generation is incomplete")
        return UnitGeneration(self.unit, state, fields['InvocationID'], cgroup,
                              main_pid, processes)

    def stop(self) -> None:
        self._run('stop', self.unit)

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
