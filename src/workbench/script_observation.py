"""Source-only observation boundary for externally scheduled commands (#189).

The caller's scheduler owns dispatch. This module neither schedules commands nor
writes Workbench's job ledger. A sink is supplied by the local, source-bound
adapter; no network transport or notification delivery is built in.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable, Mapping, Sequence

ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z", re.ASCII)
OPAQUE_ID = re.compile(r"[a-f0-9]{32}\Z", re.ASCII)
MAX_EVENT_BYTES = 4096
REPORT_WAIT_SECONDS = 0.1


@dataclass(frozen=True)
class ScriptIdentity:
    script_id: str
    revision: str
    adapter_id: str

    def __post_init__(self):
        for value in (self.script_id, self.revision, self.adapter_id):
            if not isinstance(value, str) or not ID.fullmatch(value):
                raise ValueError("invalid script identity")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def observed_event(identity: ScriptIdentity, run_id: str, seq: int, kind: str,
                   data: Mapping[str, object]) -> dict:
    if kind not in {"started", "exited", "launch_failed"} or seq not in {1, 2}:
        raise ValueError("invalid observed event")
    if (seq == 1) != (kind == "started"):
        raise ValueError("invalid observed event order")
    event = {"protocol": "script-observation.v1", "event_id": uuid.uuid4().hex,
            "script_id": identity.script_id, "script_revision": identity.revision,
            "adapter_id": identity.adapter_id, "run_id": run_id, "seq": seq,
            "occurred_at": _stamp(), "kind": kind, "data": dict(data)}
    validate_event(event, identity)
    return event


def validate_event(event: Mapping[str, object], identity: ScriptIdentity) -> None:
    """Reject malformed or source-mismatched observations before projection.

    ``identity`` must come from protected local registration, not from the
    untrusted event. This validates content, not transport authentication.
    """
    if not isinstance(event, Mapping) or set(event) != {"protocol", "event_id", "script_id",
            "script_revision", "adapter_id", "run_id", "seq", "occurred_at", "kind", "data"}:
        raise ValueError("invalid observation fields")
    if event["protocol"] != "script-observation.v1" or any(
        event[field] != expected for field, expected in (
            ("script_id", identity.script_id), ("script_revision", identity.revision),
            ("adapter_id", identity.adapter_id))
    ):
        raise ValueError("observation source mismatch")
    if any(not isinstance(event[field], str) or not OPAQUE_ID.fullmatch(event[field])
           for field in ("event_id", "run_id")):
        raise ValueError("invalid observation identity")
    stamp = event["occurred_at"]
    if not isinstance(stamp, str) or len(stamp) > 40 or not stamp.endswith("Z"):
        raise ValueError("invalid observation time")
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid observation time") from None
    kind, seq, data = event["kind"], event["seq"], event["data"]
    if (not isinstance(kind, str) or not isinstance(seq, int) or isinstance(seq, bool)
            or not isinstance(data, Mapping)
            or (kind, seq) not in {("started", 1), ("exited", 2), ("launch_failed", 2)}):
        raise ValueError("invalid observation kind")
    if kind == "started":
        valid_data = set(data) == {"pid_known"} and data["pid_known"] is True
    elif kind == "launch_failed":
        valid_data = (set(data) == {"error"} and isinstance(data["error"], str)
                      and 0 < len(data["error"]) <= 80)
    else:
        code, signum, duration = data.get("exit_code"), data.get("signal"), data.get("duration_ms")
        valid_data = (set(data) == {"exit_code", "signal", "duration_ms"}
                      and isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
                      and ((isinstance(code, int) and not isinstance(code, bool) and code >= 0 and signum is None)
                           or (code is None and isinstance(signum, int) and not isinstance(signum, bool) and signum >= 1)))
    if not valid_data:
        raise ValueError("invalid observation data")
    try:
        if len(json.dumps(event, separators=(",", ":")).encode()) > MAX_EVENT_BYTES:
            raise ValueError("oversized observation")
    except (TypeError, OverflowError):
        raise ValueError("invalid observation encoding") from None


def run_observed(identity: ScriptIdentity, argv: Sequence[str],
                 publish: Callable[[dict], None], *, cwd: str | None = None,
                 env: Mapping[str, str] | None = None) -> int:
    """Run a fixed argv with inherited streams/stdin; reporting is best effort.

    This is an opt-in wrapper callable, not a scheduler or retry mechanism.
    ``publish`` must be bound to ``identity`` by the future local adapter; a
    payload alone is never proof of source identity. No shell or timeout is used.
    """
    if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise ValueError("argv must contain nonempty strings")
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("signal forwarding requires the main thread")
    run_id = uuid.uuid4().hex
    started = time.monotonic()

    def report(seq: int, kind: str, data: Mapping[str, object]) -> bool:
        event = observed_event(identity, run_id, seq, kind, data)
        done = threading.Event()
        succeeded = []

        def deliver():
            try:
                publish(event)
                succeeded.append(True)
            except Exception:
                pass
            finally:
                done.set()

        try:
            threading.Thread(target=deliver, daemon=True).start()
        except Exception:
            return False
        return done.wait(REPORT_WAIT_SECONDS) and bool(succeeded)

    child = None
    pending_signals = []

    def forward(signum, _frame):
        if child is None:
            pending_signals.append(signum)
        else:
            # Popen.send_signal checks whether its exact child has been reaped.
            child.send_signal(signum)

    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
        try:
            child = subprocess.Popen(list(argv), cwd=cwd, env=None if env is None else dict(env))
        except OSError as exc:
            report(2, "launch_failed", {"error": type(exc).__name__})
            return 127
        for signum in pending_signals:
            child.send_signal(signum)
        pending_signals.clear()
        started_reported = report(1, "started", {"pid_known": True})
        result = child.wait()
        if started_reported:
            report(2, "exited", {"exit_code": result if result >= 0 else None,
                                 "signal": -result if result < 0 else None,
                                 "duration_ms": round((time.monotonic() - started) * 1000)})
        return result if result >= 0 else 128 - result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def read_status(events: Sequence[Mapping[str, object]], *, identity: ScriptIdentity,
                received_at: datetime | None,
                now: datetime, freshness_seconds: int) -> dict:
    """Small redacted read projection; missing evidence remains unknown."""
    if freshness_seconds <= 0:
        raise ValueError("invalid freshness window")
    for event in events:
        validate_event(event, identity)
    if now.tzinfo is None or (received_at is not None and received_at.tzinfo is None):
        raise ValueError("status times must be timezone aware")
    if (received_at is None or received_at > now
            or (now - received_at).total_seconds() > freshness_seconds):
        freshness = "unknown"
    else:
        freshness = "current"
    latest = max(events, key=lambda event: (
        datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")),
        event["seq"], event["event_id"]), default=None)
    if latest is None:
        state = "unknown"
    elif latest["kind"] == "started":
        state = "running_observed" if freshness == "current" else "unknown"
    elif latest["kind"] == "launch_failed":
        state = "wrapper_launch_failed"
    else:
        data = latest.get("data", {})
        state = "process_exit_zero" if data.get("exit_code") == 0 else "process_failed"
    return {"owner": "external_scheduler", "state": state, "freshness": freshness,
            "last_run_id": latest.get("run_id") if latest else None,
            "domain_outcome": "unknown", "human_acceptance": "unknown"}


def rgb_cue(event: Mapping[str, object], *, identity: ScriptIdentity) -> dict | None:
    """Produce a semantic cue request; RGB transport and routing are separate."""
    validate_event(event, identity)
    if event.get("kind") not in {"exited", "launch_failed"}:
        return None
    data = event.get("data", {})
    failed = event["kind"] == "launch_failed" or data.get("exit_code") != 0
    return {"protocol": "rgb-producer.v1-proposal", "event_id": event["event_id"],
            "source_id": event["script_id"], "correlation_id": event["run_id"],
            "origin_id": event["event_id"], "occurred_at": event["occurred_at"],
            "cue_id": "script.process_failed" if failed else "script.process_exited",
            "status": "failed" if failed else "completed", "severity": "warning" if failed else "info",
            "provenance": "known", "ttl_seconds": 300}
