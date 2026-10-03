"""Validate trusted, boot-bound workload-scope evidence for GPU leases.

The source is a supervised local runtime supplied during a later integration.
No caller payload may choose a scope, process generation, or completion state.
"""

from dataclasses import dataclass
import base64
import json
import math
from typing import Protocol


@dataclass(frozen=True)
class ProcessGeneration:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class ScopeBinding:
    boot_id: str
    scope_id: str
    generation: str
    cgroup_inode: int
    root: ProcessGeneration

    def owner_key(self) -> str:
        """Opaque persisted owner identity, created only by a trusted resolver."""
        data = {'boot': self.boot_id, 'scope': self.scope_id,
                'generation': self.generation, 'inode': self.cgroup_inode,
                'root_pid': self.root.pid, 'root_start': self.root.start_ticks}
        raw = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > 512:
            raise ValueError("Scope identity is too large")
        return 'scope1.' + base64.urlsafe_b64encode(raw).decode().rstrip('=')

    @classmethod
    def from_owner_key(cls, value: str) -> 'ScopeBinding':
        try:
            if not isinstance(value, str) or not value.startswith('scope1.') or len(value) > 700:
                raise ValueError("Invalid scope owner")
            encoded = value[7:]
            data = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4),
                                               altchars=b'-_', validate=True))
            if set(data) != {'boot', 'scope', 'generation', 'inode', 'root_pid', 'root_start'}:
                raise ValueError("Invalid scope fields")
            if any(not isinstance(data[name], str) or not data[name] or len(data[name]) > 180
                   for name in ('boot', 'scope', 'generation')):
                raise ValueError("Invalid scope identity")
            if any(type(data[name]) is not int or data[name] <= 0
                   for name in ('inode', 'root_pid', 'root_start')):
                raise ValueError("Invalid process identity")
            binding = cls(data['boot'], data['scope'], data['generation'], data['inode'],
                          ProcessGeneration(data['root_pid'], data['root_start']))
            if binding.owner_key() != value:
                raise ValueError("Noncanonical scope identity")
            return binding
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid scope owner") from exc


@dataclass(frozen=True)
class ScopeObservation:
    binding: ScopeBinding
    observed_at: float  # Boot-scoped monotonic time.
    members: tuple[ProcessGeneration, ...]
    ended: bool | None  # True requires authoritative full-scope completion.


class TrustedScopeSource(Protocol):
    def boot_id(self) -> str: ...
    def observe(self, binding: ScopeBinding) -> ScopeObservation | None: ...
    def process_start_ticks(self, pid: int) -> int | None: ...


class SupervisedScopeProbe:
    def __init__(self, source: TrustedScopeSource, clock, max_age_seconds: float = 1):
        if not (0 < max_age_seconds <= 5):
            raise ValueError("Scope evidence freshness must be bounded")
        self.source = source
        self.clock = clock
        self.max_age_seconds = max_age_seconds

    def _fresh(self, owner: str) -> ScopeObservation | None:
        try:
            binding = ScopeBinding.from_owner_key(owner)
            if binding.boot_id != self.source.boot_id():
                return None
            observed = self.source.observe(binding)
            now = self.clock()
            if (observed is None or observed.binding != binding or
                    not math.isfinite(observed.observed_at) or
                    not 0 <= now - observed.observed_at <= self.max_age_seconds or
                    type(observed.ended) not in (bool, type(None)) or
                    len(observed.members) > 4096):
                return None
            if len({p.pid for p in observed.members}) != len(observed.members):
                return None
            for member in observed.members:
                if (member.pid <= 0 or member.start_ticks <= 0 or
                        self.source.process_start_ticks(member.pid) != member.start_ticks):
                    return None
            return observed
        except Exception:
            # A supervisor/proc sampling fault is missing evidence, never a
            # reason to reuse an old PID attribution or clear a stale hold.
            return None

    def permitted_gpu_pids(self, active_leases) -> set[int] | None:
        """Only live members of scopes bound to current durable leases."""
        permitted = set()
        for lease in active_leases:
            observed = self._fresh(lease.owner)
            if observed is None:
                return None
            if observed.ended is True and observed.members:
                return None
            permitted.update(member.pid for member in observed.members)
        return permitted

    def ended(self, owner: str) -> bool | None:
        observed = self._fresh(owner)
        if observed is None:
            return None
        if observed.ended is True:
            return True if not observed.members else None
        return False if observed.ended is False else None
