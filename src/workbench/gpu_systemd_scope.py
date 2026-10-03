"""One exact user-systemd service as a supervised request workload.

The transient service starts an idle request worker. The worker is the UDS
peer and may start Ollama children only after its lease is granted. This
source observes that service's recursive cgroup, not a guessed process tree.
It does not create, start, or stop units.
"""

import os
from pathlib import Path
import time

from .gpu_scope import ProcessGeneration, ScopeBinding, ScopeObservation
from .gpu_unit_controller import EvidenceUnavailable, UserUnit


class SystemdRequestScopeSource:
    def __init__(self, unit: UserUnit, *, cgroup_root: Path = Path('/sys/fs/cgroup'),
                 boot_reader=None, start_ticks=None, clock=time.monotonic):
        self.unit = unit
        self.cgroup_root = Path(cgroup_root)
        self._boot_reader = boot_reader or (lambda: Path('/proc/sys/kernel/random/boot_id').read_text())
        self._start_ticks = start_ticks or unit._start_ticks
        self.clock = clock

    def boot_id(self) -> str:
        value = self._boot_reader().strip()
        if not value or len(value) > 180:
            raise EvidenceUnavailable('Boot identity unavailable')
        return value

    def process_start_ticks(self, pid: int) -> int | None:
        return self._start_ticks(pid)

    def _directory(self, cgroup: str) -> Path:
        if (not cgroup.startswith('/user.slice/') or '..' in cgroup.split('/') or
                not cgroup.endswith('/' + self.unit.unit)):
            raise EvidenceUnavailable('Scope cgroup is not the exact user unit')
        return self.cgroup_root / cgroup.lstrip('/')

    def _active_binding(self):
        current = self.unit.snapshot()
        if (current.unit != self.unit.unit or current.state != 'active' or
                not current.invocation or not current.cgroup or current.main_pid <= 0):
            raise EvidenceUnavailable('Request unit is not active')
        root = next((p for p in current.processes if p.pid == current.main_pid), None)
        if root is None or self.process_start_ticks(root.pid) != root.start_ticks:
            raise EvidenceUnavailable('Request root process is not bound')
        inode = self._directory(current.cgroup).stat().st_ino
        if inode <= 0:
            raise EvidenceUnavailable('Request cgroup inode unavailable')
        binding = ScopeBinding(self.boot_id(), current.cgroup, current.invocation,
                               inode, ProcessGeneration(root.pid, root.start_ticks))
        return binding, current

    def owner_for_peer(self, pid: int, uid: int, _gid: int) -> str:
        """Bind SO_PEERCRED to one current cgroup generation, with a resample."""
        if uid != os.getuid() or pid <= 0:
            raise PermissionError('Peer UID or PID is not permitted')
        try:
            binding, _ = self._active_binding()
            observed = self.observe(binding)
            peer_start = self.process_start_ticks(pid)
            if (observed is None or observed.ended is not False or
                    peer_start is None or
                    not any(p.pid == pid and p.start_ticks == peer_start
                            for p in observed.members)):
                raise PermissionError('Peer is outside the request scope')
            rebound, _ = self._active_binding()
            if rebound != binding or self.process_start_ticks(pid) != peer_start:
                raise PermissionError('Request scope changed during peer binding')
            return binding.owner_key()
        except (OSError, EvidenceUnavailable) as exc:
            raise PermissionError('Request scope evidence unavailable') from exc

    def observe(self, binding: ScopeBinding) -> ScopeObservation | None:
        try:
            if binding.boot_id != self.boot_id():
                return None
            directory = self._directory(binding.scope_id)
            current = self.unit.snapshot()
            if current.unit != self.unit.unit or current.invocation != binding.generation:
                return None
            if current.state == 'active':
                if (current.cgroup != binding.scope_id or
                        directory.stat().st_ino != binding.cgroup_inode or
                        current.main_pid != binding.root.pid or
                        self.process_start_ticks(binding.root.pid) != binding.root.start_ticks):
                    return None
                first = self.unit.cgroup_processes(binding.scope_id)
                second = self.unit.cgroup_processes(binding.scope_id)
                final = self.unit.snapshot()
                if (first is None or first != second or
                        not any(p.pid == binding.root.pid and
                                p.start_ticks == binding.root.start_ticks for p in first) or
                        final.unit != current.unit or final.state != 'active' or
                        final.invocation != binding.generation or
                        final.cgroup != binding.scope_id or
                        final.main_pid != binding.root.pid):
                    return None
                members = tuple(ProcessGeneration(p.pid, p.start_ticks) for p in first)
                return ScopeObservation(binding, self.clock(), members, False)
            if current.state != 'inactive' or current.cgroup not in ('', binding.scope_id):
                return None
            try:
                if directory.stat().st_ino != binding.cgroup_inode:
                    return None
            except FileNotFoundError:
                pass
            if (self.unit.cgroup_processes(binding.scope_id) != () or
                    self.unit.cgroup_processes(binding.scope_id) != () or
                    self.unit.generation_alive(binding.root) is not False):
                return None
            second = self.unit.snapshot()
            if (second.unit != current.unit or second.state != 'inactive' or
                    second.invocation != binding.generation):
                return None
            return ScopeObservation(binding, self.clock(), (), True)
        except (OSError, EvidenceUnavailable, ValueError):
            return None
