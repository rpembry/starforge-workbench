"""Pure host-local admission policy for a lower-priority GPU process.

An adapter owns durable storage, private IPC, client identity, and process
supervision. This module never runs commands or discovers processes itself.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
import secrets


class Observation(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Window:
    """Local daily half-open window; a reversed interval crosses midnight."""

    start_minute: int
    end_minute: int

    def __post_init__(self):
        if not (0 <= self.start_minute < 1440 and 0 <= self.end_minute < 1440):
            raise ValueError("Window minutes must be in a day")
        if self.start_minute == self.end_minute:
            raise ValueError("Use no windows for an unrestricted day")

    def contains(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("An aware local time is required")
        minute = instant.hour * 60 + instant.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


@dataclass(frozen=True)
class IdlePolicy:
    windows: tuple[Window, ...] = ()

    def allows(self, *, idle: bool, local_time: datetime, bypass_windows: bool = False) -> bool:
        if not idle:
            return False
        return bypass_windows or not self.windows or any(w.contains(local_time) for w in self.windows)


@dataclass
class Lease:
    owner: str
    token: str
    expires_at: float
    granted: bool = False
    stale: bool = False


@dataclass(frozen=True)
class AcquireRecord:
    owner: str
    request_key: str
    ttl: float
    token: str
    retain_until: float | None = None


@dataclass(frozen=True)
class PolicySnapshot:
    clock_id: str
    leases: tuple[Lease, ...]
    acquisitions: tuple[AcquireRecord, ...]


class ReservationPolicy:
    """Single-owner-controller state machine; persist snapshots before acknowledging.

    `owned_process` and `owned_context` concern only the background process.
    Other users of the GPU may legitimately hold memory. `workload_ended`
    is authoritative adapter evidence that the client's entire supervised
    workload scope has ended, not merely its parent process. Unknown scope
    state retains a stale hold after expiry.
    """

    REPLAY_SECONDS = 3600

    def __init__(self, snapshot: PolicySnapshot | None = None, *,
                 clock_id: str = 'synthetic-boot', now: float = 0):
        if not clock_id:
            raise ValueError("A boot clock identity is required")
        snapshot = snapshot or PolicySnapshot(clock_id, (), ())
        self.clock_id = clock_id
        self._leases = {lease.token: Lease(**vars(lease)) for lease in snapshot.leases}
        self._acquisitions = {(record.owner, record.request_key): record
                              for record in snapshot.acquisitions}
        if snapshot.clock_id != clock_id:
            # A monotonic timestamp from another boot is incomparable. Keep
            # active workloads fenced and give tombstones a fresh full window.
            for lease in self._leases.values():
                lease.granted = False
                lease.stale = True
                lease.expires_at = now
            self._acquisitions = {
                key: AcquireRecord(record.owner, record.request_key, record.ttl,
                                   record.token, now + self.REPLAY_SECONDS
                                   if record.retain_until is not None else None)
                for key, record in self._acquisitions.items()}

    def acquire(self, owner: str, request_key: str, now: float, ttl: float) -> Lease:
        if (not owner or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_key) or
                not (0 < ttl <= 3600)):
            raise ValueError("An owner, request key and bounded TTL are required")
        self._prune(now)
        prior = self._acquisitions.get((owner, request_key))
        if prior:
            if prior.ttl != ttl:
                raise ValueError("Request key reused with a different TTL")
            lease = self._leases.get(prior.token)
            if lease is None:
                raise ValueError("Reservation for request key has ended")
            if now >= lease.expires_at or lease.stale:
                raise ValueError("Reservation for request key awaits expiry reconciliation")
            return Lease(**vars(lease))
        token = secrets.token_urlsafe(32)
        lease = Lease(owner, token, now + ttl)
        self._leases[token] = lease
        self._acquisitions[(owner, request_key)] = AcquireRecord(
            owner, request_key, ttl, token)
        return Lease(**vars(lease))

    def _prune(self, now: float) -> None:
        for key, record in tuple(self._acquisitions.items()):
            if record.retain_until is not None and now >= record.retain_until:
                del self._acquisitions[key]

    def _retire(self, token: str, now: float) -> None:
        for key, record in tuple(self._acquisitions.items()):
            if record.token == token:
                self._acquisitions[key] = AcquireRecord(
                    record.owner, record.request_key, record.ttl,
                    record.token, now + self.REPLAY_SECONDS)

    def _get(self, owner: str, token: str) -> Lease:
        lease = self._leases.get(token)
        if lease is None or not secrets.compare_digest(lease.owner, owner):
            raise PermissionError("Unknown reservation")
        return lease

    def renew(self, owner: str, token: str, now: float, ttl: float) -> Lease:
        lease = self._get(owner, token)
        if not (0 < ttl <= 3600) or now >= lease.expires_at or lease.stale:
            raise ValueError("Reservation is expired or TTL is invalid")
        # A wall-clock rollback must never shorten an already acknowledged hold.
        lease.expires_at = max(lease.expires_at, now + ttl)
        return Lease(**vars(lease))

    def view(self, owner: str, token: str) -> Lease:
        """Return a detached status value for the authenticated owner."""
        return Lease(**vars(self._get(owner, token)))

    def release(self, owner: str, token: str, now: float) -> None:
        self._get(owner, token)
        del self._leases[token]
        self._retire(token, now)

    def reconcile(self, *, now: float, owned_process: Observation,
                  owned_context: Observation, workload_ended, idle_allowed: bool) -> str:
        """Return STOP, HOLD, or START; never a process-control instruction to a client.

        The adapter acts on STOP and calls reconcile again after observing exit.
        It must persist updated leases before exposing any newly granted result.
        """
        for token, lease in tuple(self._leases.items()):
            if now >= lease.expires_at:
                if workload_ended(lease.owner) is True:
                    del self._leases[token]
                    self._retire(token, now)
                else:
                    lease.stale = True
        self._prune(now)
        if self._leases:
            if owned_process is Observation.PRESENT or owned_context is Observation.PRESENT:
                for lease in self._leases.values():
                    lease.granted = False
                return "STOP"
            if owned_process is Observation.ABSENT and owned_context is Observation.ABSENT:
                for lease in self._leases.values():
                    if not lease.stale:
                        lease.granted = True
            else:
                for lease in self._leases.values():
                    lease.granted = False
            return "HOLD"
        if owned_process is Observation.UNKNOWN or owned_context is Observation.UNKNOWN:
            return "HOLD"
        if owned_process is Observation.PRESENT or owned_context is Observation.PRESENT:
            return "HOLD" if idle_allowed else "STOP"
        return "START" if idle_allowed else "HOLD"

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            self.clock_id,
            tuple(Lease(**vars(lease)) for lease in self._leases.values()),
            tuple(self._acquisitions.values()))
