"""Source-only observation boundary for externally scheduled commands (#189).

The caller's scheduler owns dispatch. This module neither schedules commands nor
writes Workbench's job ledger. A sink is supplied by the local, source-bound
adapter; no network transport or notification delivery is built in.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ScriptIdentity:
    script_id: str
    revision: str
    adapter_id: str

    def __post_init__(self):
        for value in (self.script_id, self.revision, self.adapter_id):
            if not value or len(value) > 128 or not all(c.isalnum() or c in "._-" for c in value):
                raise ValueError("invalid script identity")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def observed_event(identity: ScriptIdentity, run_id: str, seq: int, kind: str,
                   data: Mapping[str, object]) -> dict:
    if kind not in {"started", "exited", "launch_failed"} or seq not in {1, 2}:
        raise ValueError("invalid observed event")
    if (seq == 1) != (kind == "started"):
        raise ValueError("invalid observed event order")
    return {"protocol": "script-observation.v1", "event_id": uuid.uuid4().hex,
            "script_id": identity.script_id, "script_revision": identity.revision,
            "adapter_id": identity.adapter_id, "run_id": run_id, "seq": seq,
            "occurred_at": _stamp(), "kind": kind, "data": dict(data)}


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

    def report(seq: int, kind: str, data: Mapping[str, object]) -> None:
        try:
            publish(observed_event(identity, run_id, seq, kind, data))
        except Exception:
            # Observation failure cannot replace the scheduler's process result.
            pass

    try:
        child = subprocess.Popen(list(argv), cwd=cwd, env=None if env is None else dict(env))
    except OSError as exc:
        report(2, "launch_failed", {"error": type(exc).__name__})
        return 127
    report(1, "started", {"pid_known": True})
    previous = {}

    def forward(signum, _frame):
        # Popen retains exact-child ownership; it checks for a reaped process.
        if child.poll() is None:
            child.send_signal(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
        result = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    report(2, "exited", {"exit_code": result if result >= 0 else None,
                         "signal": -result if result < 0 else None,
                         "duration_ms": round((time.monotonic() - started) * 1000)})
    return result if result >= 0 else 128 - result


def read_status(events: Sequence[Mapping[str, object]], *, received_at: datetime | None,
                now: datetime, freshness_seconds: int) -> dict:
    """Small redacted read projection; missing evidence remains unknown."""
    if freshness_seconds <= 0:
        raise ValueError("invalid freshness window")
    if received_at is None or (now - received_at).total_seconds() > freshness_seconds:
        freshness = "unknown"
    else:
        freshness = "current"
    valid = [event for event in events if event.get("protocol") == "script-observation.v1"
             and event.get("kind") in {"started", "exited", "launch_failed"}]
    latest = max(valid, key=lambda event: str(event.get("occurred_at", "")), default=None)
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


def rgb_cue(event: Mapping[str, object]) -> dict | None:
    """Produce a semantic cue request; RGB transport and routing are separate."""
    if event.get("protocol") != "script-observation.v1":
        raise ValueError("unsupported script observation")
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
