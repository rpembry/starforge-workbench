"""Coordinator-side durable command relay for the independent supervisor.

This module never opens either journal or invokes Docker. A caller drives tick()
under a single coordinator process. Runtime result/log/artifact evidence remains a
separate reviewed contract; a stopped noncancelled attempt stays finalizing.
"""
from __future__ import annotations

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

    _allowed = frozenset({"acquire", "renew", "launch", "reconcile", "cancel", "inspect"})

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
        try:
            self._check_socket()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(wire)
                with connection.makefile("rb") as stream:
                    response = stream.readline(131_073)
        except (OSError, TimeoutError) as exc:
            raise Unavailable("supervisor control unavailable; operation outcome unknown") from exc
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
        if (job["visibility"] == "fresh" and
                attempt["phase"] == state and
                (state != "stopped" or job["phase"] in {"finalizing", "terminal"})):
            return job
        return self.store.observe(job["id"], attempt_id=item["id"],
                                  incarnation=item["incarnation"],
                                  observation_seq=item["observation_seq"],
                                  phase="stopped" if state == "stopped" else "running",
                                  supervisor_id=self.lease["supervisor_id"],
                                  runtime_id=item["runtime_id"],
                                  stopped=state == "stopped")

    def _command(self, op: dict):
        job = self.store.get(op["job_id"])
        attempt_id = job["attempt_id"]
        if not attempt_id:
            return
        if op["kind"] in {"admit", "retry"}:
            if job["intent"] == "cancel":
                # A cancel committed before supervisor journal creation must
                # never be transformed into a new runtime launch.
                self.store.set_command_status(op["id"], "unknown")
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
            item = self.control.call("reconcile", attempt_id=attempt_id)
            self._observe(job, item)
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
            item = self.control.call("cancel", attempt_id=attempt_id,
                                     controller=self.controller,
                                     generation=self.lease["generation"],
                                     operation_id=op["id"])
            if item["state"] in {"launch_pending", "launch_calling", "unknown"}:
                self.store.set_command_status(op["id"], "unknown")
                return
            item = self.control.call("reconcile", attempt_id=attempt_id)
            self._observe(job, item)
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
                item = self.control.call("reconcile", attempt_id=job["attempt_id"])
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
