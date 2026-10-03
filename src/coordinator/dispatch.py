"""Coordinator-side durable command relay for the independent supervisor.

This module never opens either journal or invokes Docker. A caller drives tick()
under a single coordinator process. Runtime result/log/artifact evidence remains a
separate reviewed contract; a stopped noncancelled attempt stays finalizing.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import socket
import time
import uuid

from . import Conflict, CoordinatorStore, Unavailable


class ControlError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SupervisorControl:
    """Bounded client for supervisor_service.py's owner-protected control.sock."""

    _allowed = frozenset({"acquire", "renew", "launch", "abandon", "reconcile",
                          "cancel", "collect", "read_artifact", "inspect"})

    def __init__(self, socket_path: str | Path, *, timeout: float = 5):
        if not 0 < timeout <= 30:
            raise ValueError("invalid supervisor timeout")
        self.path = str(Path(socket_path).absolute())
        self.timeout = timeout

    def _check_socket(self):
        path = Path(self.path)
        parent = path.parent
        if (path.is_symlink() or not path.is_socket() or path.stat().st_uid != os.getuid() or
                path.stat().st_mode & 0o077 or parent.is_symlink() or
                parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077):
            raise Unavailable("protected supervisor control socket unavailable")

    def call(self, method: str, **args):
        if method not in self._allowed:
            raise ValueError("unsupported supervisor control operation")
        wire = json.dumps({"method": method, "args": args}, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(wire) > 131_072:
            raise ValueError("supervisor request exceeds limit")
        directory_fd = None
        try:
            self._check_socket()
            path = Path(self.path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            address = f"/proc/self/fd/{directory_fd}/{path.name}"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(address)
                connection.sendall(wire)
                with connection.makefile("rb") as stream:
                    response = stream.readline(131_073)
        except (OSError, TimeoutError) as exc:
            raise Unavailable("supervisor control unavailable; operation outcome unknown") from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
        if not response or len(response) > 131_072 or not response.endswith(b"\n"):
            raise Unavailable("supervisor response incomplete; operation outcome unknown")
        try:
            body = json.loads(response)
        except ValueError as exc:
            raise Unavailable("supervisor response invalid; operation outcome unknown") from exc
        if (not isinstance(body, dict) or not isinstance(body.get("ok"), bool) or
                set(body) != ({"ok", "result"} if body["ok"] else {"ok", "error"})):
            raise Unavailable("supervisor response invalid; operation outcome unknown")
        if not body["ok"]:
            raise ControlError(body["error"])
        return body["result"]


class Dispatcher:
    """One coordinator controller. tick() is safe to repeat after lost responses."""

    def __init__(self, store: CoordinatorStore, control, *, controller: str | None = None,
                 clock=time.monotonic):
        self.store = store
        self.control = control
        self.controller = controller or uuid.uuid4().hex
        self.clock = clock
        self.lease: dict | None = None
        self.renew_at = 0.0
        self.last_error: str | None = None

    def _lease(self):
        if self.lease is None:
            self.lease = self.control.call("acquire", controller=self.controller, lease_seconds=15)
            self.renew_at = self.clock() + 5
        elif self.clock() >= self.renew_at:
            self.control.call("renew", controller=self.controller,
                              generation=self.lease["generation"], lease_seconds=15)
            self.renew_at = self.clock() + 5

    @staticmethod
    def _plan(job: dict) -> dict:
        attempt = next(a for a in job["attempts"] if a["id"] == job["attempt_id"])
        spec = job["spec"]
        return {key: value for key, value in {
            "job_id": job["id"], "attempt_id": attempt["id"],
            "incarnation": attempt["incarnation"], "worker_type": spec["worker_type"],
            "profile_ref": spec["profile_ref"], "workspace_ref": spec["workspace_ref"],
            "payload": spec["payload"], "deadline_seconds": spec["deadline_seconds"],
            "orphan_policy": spec["orphan_policy"]}.items()}

    def _owned(self, job: dict, item: dict) -> bool:
        attempt = next((a for a in job["attempts"] if a["id"] == job["attempt_id"]), None)
        return bool(attempt and item.get("id") == attempt["id"] and
                    item.get("job_id") == job["id"] and
                    item.get("incarnation") == attempt["incarnation"] and
                    item.get("plan") == self._plan(job))

    def _observe(self, job: dict, item: dict):
        if not self._owned(job, item):
            raise Conflict("supervisor attempt identity mismatch")
        state = item["state"]
        if state == "unknown":
            return self.store.mark_visibility_unknown(job["id"], expected_version=job["version"])
        if state not in {"running", "stopped"} or item["observation_seq"] < 1:
            return job
        attempt = next(a for a in job["attempts"] if a["id"] == item["id"])
        if (job["visibility"] == "fresh" and attempt["phase"] == state and
                job["phase"] == "terminal"):
            return job
        evidence = None
        no_start_reason = item.get("no_start_reason")
        if no_start_reason is not None and state != "stopped":
            raise Conflict("supervisor no-start state mismatch")
        if state == "stopped" and item["runtime_id"] is None and not no_start_reason:
            raise Conflict("stopped attempt lacks runtime or no-start evidence")
        if state == "stopped" and no_start_reason:
            if item["runtime_id"] is not None or no_start_reason not in {
                    "workspace_setup_failed", "prelaunch_abandon"}:
                raise Conflict("supervisor no-start identity mismatch")
            evidence = self.control.call("collect", attempt_id=item["id"],
                                         controller=self.controller,
                                         generation=self.lease["generation"])
            if (not isinstance(evidence, dict) or evidence.get("no_start") is not True or
                    evidence.get("no_start_reason") != no_start_reason or
                    evidence.get("runtime_id") is not None or
                    evidence.get("exit_code") is not None or
                    evidence.get("result_ok") is not False or
                    "artifact_manifest" in evidence or
                    type(evidence.get("observation_seq")) is not int or
                    evidence["observation_seq"] < item["observation_seq"]):
                raise Conflict("supervisor no-start evidence mismatch")
        elif state == "stopped" and item["runtime_id"] and job["intent"] != "cancel":
            try:
                evidence = self.control.call("collect", attempt_id=item["id"],
                                             controller=self.controller,
                                             generation=self.lease["generation"])
            except ControlError as exc:
                if exc.code not in {"OwnershipUnknown", "RecoveryUncertain"}:
                    raise
                # Positive stop is known; result remains unverified.
        if evidence is not None:
            if (evidence.get("job_id") != job["id"] or
                    evidence.get("attempt_id") != item["id"] or
                    evidence.get("incarnation") != item["incarnation"] or
                    evidence.get("runtime_id") != item["runtime_id"] or
                    evidence.get("supervisor_id") != self.lease["supervisor_id"] or
                    evidence.get("phase") != "stopped" or evidence.get("stopped") is not True):
                raise Conflict("supervisor evidence identity mismatch")
        if no_start_reason:
            return self.store.record_no_start(
                job["id"], attempt_id=item["id"], incarnation=item["incarnation"],
                observation_seq=evidence["observation_seq"],
                supervisor_id=evidence["supervisor_id"], reason=no_start_reason)
        if (job["visibility"] == "fresh" and attempt["phase"] == state and
                (state == "running" or evidence is None and job["phase"] == "finalizing")):
            return job
        return self.store.observe(job["id"], attempt_id=item["id"],
                                  incarnation=item["incarnation"],
                                  observation_seq=(evidence or item)["observation_seq"],
                                  phase="stopped" if state == "stopped" else "running",
                                  supervisor_id=self.lease["supervisor_id"],
                                  runtime_id=item["runtime_id"],
                                  stopped=state == "stopped",
                                  exit_code=evidence["exit_code"] if evidence else None,
                                  result_ok=evidence["result_ok"] if evidence else None)

    def _command(self, op: dict):
        job = self.store.get(op["job_id"])
        attempt_id = job["attempt_id"]
        if not attempt_id:
            return
        if op["kind"] in {"admit", "retry"}:
            if job["intent"] == "cancel":
                # Canonical launch op ID creates a durable no-start tombstone or
                # stops the exact existing runtime. Delayed launch cannot win.
                item = self.control.call("abandon", plan=self._plan(job),
                                         controller=self.controller,
                                         generation=self.lease["generation"],
                                         operation_id=op["id"])
                observed = self._observe(job, item)
                if item["state"] == "stopped" and observed["visibility"] == "fresh":
                    self.store.set_command_status(op["id"], "confirmed")
                return
            try:
                item = self.control.call("inspect", attempt_id=attempt_id)
            except ControlError as exc:
                if exc.code != "KeyError":
                    raise
                item = self.control.call("launch", plan=self._plan(job),
                                         controller=self.controller,
                                         generation=self.lease["generation"],
                                         operation_id=op["id"])
            if not self._owned(job, item):
                raise Conflict("supervisor attempt identity mismatch")
            item = self.control.call("reconcile", attempt_id=attempt_id,
                                     controller=self.controller,
                                     generation=self.lease["generation"])
            observed = self._observe(job, item)
            if item["state"] in {"running", "stopped"} and observed["visibility"] == "fresh":
                self.store.set_command_status(op["id"], "confirmed")
        elif op["kind"] == "reattach":
            original = json.loads(op["response"])["attempt_id"]
            if original != attempt_id:
                # A later explicit retry already proved the predecessor stopped.
                # Never reattach this old command to the new attempt.
                self.store.set_command_status(op["id"], "confirmed")
                return
            try:
                item = self.control.call("inspect", attempt_id=attempt_id)
            except ControlError as exc:
                if exc.code == "KeyError":
                    self.store.set_command_status(op["id"], "unknown")
                    return
                raise
            if not self._owned(job, item):
                raise Conflict("supervisor attempt identity mismatch")
            item = self.control.call("reconcile", attempt_id=attempt_id,
                                     controller=self.controller,
                                     generation=self.lease["generation"])
            observed = self._observe(job, item)
            if item["state"] in {"running", "stopped"} and observed["visibility"] == "fresh":
                self.store.set_command_status(op["id"], "confirmed")
        elif op["kind"] == "cancel":
            try:
                item = self.control.call("inspect", attempt_id=attempt_id)
            except ControlError as exc:
                if exc.code == "KeyError":
                    self.store.set_command_status(op["id"], "unknown")
                    return
                raise
            if not self._owned(job, item):
                raise Conflict("supervisor attempt identity mismatch")
            if item["state"] == "stopped":
                observed = self._observe(job, item)
                if observed["visibility"] == "fresh":
                    self.store.set_command_status(op["id"], "confirmed")
                return
            item = self.control.call("cancel", attempt_id=attempt_id,
                                     controller=self.controller,
                                     generation=self.lease["generation"],
                                     operation_id=op["id"])
            if item["state"] in {"launch_pending", "launch_calling", "unknown"}:
                self.store.set_command_status(op["id"], "unknown")
                return
            item = self.control.call("reconcile", attempt_id=attempt_id,
                                     controller=self.controller,
                                     generation=self.lease["generation"])
            observed = self._observe(job, item)
            if item["state"] == "stopped" and observed["visibility"] == "fresh":
                self.store.set_command_status(op["id"], "confirmed")
        else:
            raise Conflict("unsupported pending coordinator command")

    def _mark_active_unknown(self):
        for job in self.store.list(1000):
            if job["phase"] in {"active", "finalizing"} and job["visibility"] != "unknown":
                self.store.mark_visibility_unknown(job["id"], expected_version=job["version"])

    def tick(self) -> dict:
        """Relay durable commands, then reserve at most one further queued job.

        Failure leaves pending/unknown commands for the same operation identity
        to reconcile on the next tick. No inference of launch, stop, or success.
        """
        try:
            self._lease()
        except (ControlError, Unavailable) as exc:
            if isinstance(exc, ControlError) and exc.code == "Fenced":
                self.lease = None
            self._mark_active_unknown()
            raise
        processed = 0
        for op in self.store.pending_commands(100):
            try:
                self._command(op)
                processed += 1
            except (ControlError, Unavailable) as exc:
                if isinstance(exc, ControlError) and exc.code == "Fenced":
                    self.lease = None
                self.store.set_command_status(op["id"], "unknown")
                self._mark_active_unknown()
                raise
        # Reconcile already owned active attempts too; no event is emitted for
        # an unchanged healthy observation. Unknown visibility is not failure.
        for job in self.store.list(1000):
            if job["phase"] not in {"active", "finalizing"} or not job["attempt_id"]:
                continue
            try:
                item = self.control.call("reconcile", attempt_id=job["attempt_id"],
                                         controller=self.controller,
                                         generation=self.lease["generation"])
            except ControlError as exc:
                if exc.code == "KeyError":
                    continue  # prelaunch intent; never invent runtime evidence
                self._mark_active_unknown()
                raise
            except Unavailable:
                self._mark_active_unknown()
                raise
            self._observe(job, item)
        admitted = self.store.admit_next(principal="coordinator-dispatch",
                                         key=uuid.uuid4().hex)
        return {"processed": processed, "admitted": admitted["id"] if admitted else None}


class SupervisorEvidence:
    """Post-stop evidence adapter for the API; never reads Docker or journals."""

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher

    def health(self) -> dict:
        return {"state": "unavailable" if self.dispatcher.last_error else
                "ready" if self.dispatcher.lease else "unknown"}

    def capabilities(self) -> dict:
        return {"worker_types": sorted(self.dispatcher.store.policy.worker_types),
                "reattach": True, "post_stop_logs": True, "post_stop_artifacts": True,
                "live_logs": False, "checkpoint_resume": False}

    def _call(self, method: str, **kwargs):
        try:
            return self.dispatcher.control.call(method, **kwargs)
        except ControlError as exc:
            if exc.code == "KeyError":
                raise KeyError(method) from exc
            if exc.code == "ValueError":
                raise ValueError("invalid supervisor evidence request") from exc
            raise Unavailable("supervisor evidence unavailable") from exc

    def _evidence(self, job: dict, attempt_id: str) -> dict:
        try:
            self.dispatcher._lease()
        except ControlError as exc:
            raise Unavailable("supervisor control unavailable") from exc
        attempt = next((a for a in job["attempts"] if a["id"] == attempt_id), None)
        if attempt is None:
            raise KeyError(attempt_id)
        if not attempt["stopped"]:
            raise Unavailable("post-stop evidence unavailable while attempt is active")
        if not attempt["runtime_id"] and not attempt.get("no_start_reason"):
            raise Unavailable("no-start evidence unavailable")
        data = self._call("collect", attempt_id=attempt_id,
                                            controller=self.dispatcher.controller,
                                            generation=self.dispatcher.lease["generation"])
        if (data.get("job_id") != job["id"] or data.get("attempt_id") != attempt_id or
                data.get("incarnation") != attempt["incarnation"] or
                data.get("runtime_id") != attempt["runtime_id"] or
                data.get("supervisor_id") != attempt["supervisor_id"]):
            raise Conflict("supervisor artifact evidence identity mismatch")
        if attempt.get("no_start_reason"):
            if (data.get("no_start") is not True or
                    data.get("no_start_reason") != attempt["no_start_reason"] or
                    data.get("phase") != "stopped" or data.get("stopped") is not True or
                    data.get("runtime_id") is not None or data.get("exit_code") is not None or
                    data.get("result_ok") is not False or "artifact_manifest" in data):
                raise Conflict("supervisor no-start evidence mismatch")
            return {**data, "artifact_manifest": {"files": [], "execution_ok": None}}
        return data

    def artifacts(self, job: dict, attempt_id: str) -> dict:
        evidence = self._evidence(job, attempt_id)
        return {"job_id": job["id"], "attempt_id": attempt_id,
                "items": evidence["artifact_manifest"]["files"],
                "execution_ok": evidence["artifact_manifest"]["execution_ok"]}

    def artifact(self, job: dict, attempt_id: str, artifact_id: str,
                 offset: int, limit: int) -> dict:
        evidence = self._evidence(job, attempt_id)
        entry = next((item for item in evidence["artifact_manifest"]["files"]
                      if item["path"] == artifact_id), None)
        if entry is None:
            raise KeyError(artifact_id)
        chunk = self._call("read_artifact", attempt_id=attempt_id,
                           name=artifact_id, offset=offset, limit=limit)
        try:
            data = base64.b64decode(chunk["data_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise Unavailable("supervisor artifact chunk invalid") from exc
        if (chunk["attempt_id"] != attempt_id or chunk["name"] != artifact_id or
                chunk["offset"] != offset or chunk["next_offset"] != offset + len(data) or
                chunk["total"] != entry["bytes"] or chunk["sha256"] != entry["sha256"] or
                len(data) > limit):
            raise Conflict("supervisor artifact chunk identity mismatch")
        return chunk

    def logs(self, job: dict, attempt_id: str, cursor: int, limit: int) -> dict:
        evidence = self._evidence(job, attempt_id)
        entry = next((item for item in evidence["artifact_manifest"]["files"]
                      if item["path"] == "output.txt"), None)
        if entry is None:
            raise Unavailable("bounded log export unavailable")
        chunk = self._call("read_artifact", attempt_id=attempt_id,
                                             name="output.txt", offset=cursor, limit=limit)
        try:
            data = base64.b64decode(chunk["data_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise Unavailable("supervisor log chunk invalid") from exc
        if (chunk["attempt_id"] != attempt_id or chunk["name"] != "output.txt" or
                chunk["sha256"] != entry["sha256"] or chunk["total"] != entry["bytes"] or
                chunk["offset"] != cursor or chunk["next_offset"] != cursor + len(data) or
                len(data) > limit):
            raise Conflict("supervisor log evidence identity mismatch")
        return {"job_id": job["id"], "attempt_id": attempt_id,
                "cursor": cursor, "next_cursor": min(chunk["next_offset"], chunk["total"]),
                "floor": 0, "gap": cursor > chunk["total"], "total": chunk["total"],
                "sha256": chunk["sha256"], "data_base64": chunk["data_base64"],
                "text": data.decode("utf-8", errors="replace"),
                "source_window": "last_1000_lines", "possible_prefix_gap": True}
