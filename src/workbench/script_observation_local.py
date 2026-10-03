"""Bounded private observation buffer for an explicitly configured local script.

This is neither a scheduler nor Workbench's job ledger. The caller binds the
source identity from protected local configuration before writing or reading.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .script_observation import ScriptIdentity, read_status, validate_event

MAX_EVENTS = 128
MAX_TOTAL_BYTES = 512 * 1024
MAX_EVENT_BYTES = 4096
MAX_GAP_BYTES = 1024
GAP_NAME = ".observation.gap"
EVENT_NAME = re.compile(r"[a-f0-9]{32}-[12]-[a-f0-9]{32}\.json\Z")


class ObservationBufferError(Exception):
    """A local observation cannot be safely stored or read."""


@contextmanager
def _protected_directory(directory: Path, *, create_lock: bool = True):
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise ObservationBufferError("unsafe_observation_directory")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ObservationBufferError("unsafe_observation_directory")
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create_lock:
            flags |= os.O_CREAT
        lock_fd = os.open(".observation.lock", flags, 0o600, dir_fd=fd)
        try:
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600):
                raise ObservationBufferError("unsafe_observation_lock")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ObservationBufferError("observation_buffer_busy") from None
            yield fd
        finally:
            os.close(lock_fd)
    except OSError as exc:
        raise ObservationBufferError("observation_io_unavailable") from exc
    finally:
        os.close(fd)


def _files(fd: int) -> list[tuple[str, os.stat_result]]:
    records = []
    total = 0
    with os.scandir(fd) as entries:
        for entry in entries:
            name = entry.name
            if name in {".observation.lock", GAP_NAME}:
                continue
            if not EVENT_NAME.fullmatch(name):
                raise ObservationBufferError("unexpected_observation_entry")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_EVENT_BYTES):
                raise ObservationBufferError("unsafe_observation_entry")
            records.append((name, info))
            total += info.st_size
            if len(records) > MAX_EVENTS or total > MAX_TOTAL_BYTES:
                raise ObservationBufferError("observation_buffer_over_limit")
    return records


def _read(fd: int, name: str) -> bytes:
    item_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(item_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_EVENT_BYTES):
            raise ObservationBufferError("unsafe_observation_entry")
        with os.fdopen(item_fd, "rb", closefd=False) as stream:
            payload = stream.read(MAX_EVENT_BYTES + 1)
        if len(payload) > MAX_EVENT_BYTES:
            raise ObservationBufferError("oversized_observation_entry")
        return payload
    finally:
        os.close(item_fd)


def _gap(fd: int, identity: ScriptIdentity) -> tuple[int, datetime] | None:
    try:
        info = os.stat(GAP_NAME, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_GAP_BYTES):
        raise ObservationBufferError("unsafe_observation_gap")
    value = json.loads(_read(fd, GAP_NAME))
    if (not isinstance(value, dict) or set(value) != {"version", "identity", "blocked_event_id", "blocked_at"}
            or value["version"] != 1 or value["identity"] != [identity.script_id,
            identity.revision, identity.adapter_id] or not isinstance(value["blocked_event_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["blocked_event_id"])
            or not isinstance(value["blocked_at"], str) or len(value["blocked_at"]) > 40
            or not value["blocked_at"].endswith("Z")):
        raise ObservationBufferError("invalid_observation_gap")
    return info.st_mtime_ns, datetime.fromisoformat(value["blocked_at"].replace("Z", "+00:00"))


def _record_gap(fd: int, identity: ScriptIdentity, event: dict) -> None:
    # One bounded marker survives a later retention/archive of old event files.
    prior = _gap(fd, identity)  # reject an unsafe or foreign marker before replacement
    blocked_at = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    if prior is not None:
        blocked_at = max(blocked_at, prior[1])
    payload = json.dumps({"version": 1, "identity": [identity.script_id, identity.revision,
                          identity.adapter_id], "blocked_event_id": event["event_id"],
                          "blocked_at": blocked_at.isoformat().replace("+00:00", "Z")},
                         separators=(",", ":")).encode()
    if len(payload) > MAX_GAP_BYTES:
        raise ObservationBufferError("oversized_observation_gap")
    temp = ".observation-gap-" + uuid.uuid4().hex
    temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
    try:
        with os.fdopen(temp_fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, GAP_NAME, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(temp_fd)
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def write_event(directory: Path, identity: ScriptIdentity, event: dict) -> None:
    """Atomically append one validated event; exact replay is idempotent."""
    validate_event(event, identity)
    payload = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_EVENT_BYTES:
        raise ObservationBufferError("oversized_observation")
    name = f'{event["run_id"]}-{event["seq"]}-{event["event_id"]}.json'
    with _protected_directory(directory) as fd:
        records = _files(fd)
        _gap(fd, identity)
        for prior_name, _ in records:
            if prior_name.endswith(f'-{event["event_id"]}.json'):
                if prior_name == name and _read(fd, prior_name) == payload:
                    return
                raise ObservationBufferError("conflicting_observation_replay")
        if len(records) >= MAX_EVENTS or sum(info.st_size for _, info in records) + len(payload) > MAX_TOTAL_BYTES:
            _record_gap(fd, identity, event)
            raise ObservationBufferError("observation_buffer_full")
        temp = ".observation-" + uuid.uuid4().hex
        temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
        try:
            with os.fdopen(temp_fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.fsync(fd)
        finally:
            os.close(temp_fd)
            os.unlink(temp, dir_fd=fd)


def status_from_spool(directory: Path, identity: ScriptIdentity, *, now: datetime,
                      freshness_seconds: int) -> dict:
    """Return a redacted status or explicit unknown when evidence is unsafe."""
    unknown = {"owner": "external_scheduler", "state": "unknown", "freshness": "unknown",
               "last_run_id": None, "domain_outcome": "unknown", "human_acceptance": "unknown"}
    try:
        with _protected_directory(directory, create_lock=False) as fd:
            records = _files(fd)
            gap_stamp = _gap(fd, identity)
            events = []
            seen_ids = set()
            for name, info in records:
                event = json.loads(_read(fd, name))
                validate_event(event, identity)
                if name != f'{event["run_id"]}-{event["seq"]}-{event["event_id"]}.json' or event["event_id"] in seen_ids:
                    raise ObservationBufferError("conflicting_observation_record")
                seen_ids.add(event["event_id"])
                events.append((event, info.st_mtime_ns))
            by_run = {}
            for event, _ in events:
                kinds = by_run.setdefault(event["run_id"], {})
                if event["seq"] in kinds:
                    raise ObservationBufferError("conflicting_observation_sequence")
                kinds[event["seq"]] = event["kind"]
            if any(kinds.get(2) == "exited" and kinds.get(1) != "started" for kinds in by_run.values()):
                raise ObservationBufferError("observation_sequence_gap")
            # A rejected new start leaves no event to distinguish it from the
            # prior run. Never keep advertising that prior success as current.
            if (len(records) >= MAX_EVENTS
                    or sum(info.st_size for _, info in records) + MAX_EVENT_BYTES > MAX_TOTAL_BYTES):
                return {**unknown, "reporting": "full"}
            current_events = [(event, stamp) for event, stamp in events
                              if gap_stamp is None or (stamp > gap_stamp[0] and
                                  datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")) > gap_stamp[1])]
            if gap_stamp is not None and not current_events:
                return {**unknown, "reporting": "gap"}
            current_runs = {}
            for event, _ in current_events:
                current_runs.setdefault(event["run_id"], {})[event["seq"]] = event["kind"]
            if any(kinds.get(2) == "exited" and kinds.get(1) != "started" for kinds in current_runs.values()):
                return {**unknown, "reporting": "gap"}
            received = (datetime.fromtimestamp(max(stamp for _, stamp in current_events) / 1e9, timezone.utc)
                        if current_events else None)
            status = read_status([event for event, _ in current_events], identity=identity,
                                 received_at=received, now=now, freshness_seconds=freshness_seconds)
            status["reporting"] = "available"
            return status
    except (ObservationBufferError, OSError, ValueError, TypeError, KeyError):
        return {**unknown, "reporting": "unavailable"}
