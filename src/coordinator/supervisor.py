"""Independent host ownership journal and fenced runtime boundary.

The runtime adapter is host-owned. Its inspect/stop methods MUST target only an
exact, previously verified owned attempt; supervisor never accepts a Docker ID,
path, image, or mount from a client. This module does not contact Workbench.
"""

from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .store import Conflict, Unavailable, _json


class Fenced(Conflict):
    pass


class OwnershipUnknown(RuntimeError):
    pass


def _serialized(method):
    """Serialize authority changes and runtime mutations across service processes."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        fd = os.open(self.root / ".supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return guarded


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE meta(version INTEGER NOT NULL CHECK(version=1), supervisor_id TEXT NOT NULL,
 generation INTEGER NOT NULL, controller TEXT, lease_until REAL NOT NULL, last_wall REAL NOT NULL,
 blocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, incarnation TEXT NOT NULL,
 plan_hash TEXT NOT NULL, plan TEXT NOT NULL, generation INTEGER NOT NULL,
 launch_op TEXT NOT NULL UNIQUE, runtime_id TEXT, state TEXT NOT NULL,
 cancel INTEGER NOT NULL DEFAULT 0, deadline REAL NOT NULL,
 orphan_deadline REAL NOT NULL, policy TEXT NOT NULL, grace REAL NOT NULL,
 observation_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE operations(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
 kind TEXT NOT NULL, status TEXT NOT NULL, response TEXT);
PRAGMA user_version=1;
COMMIT;
"""


class Supervisor:
    """A separately running owner object; its tick() must be driven by its service.

    Runtime protocol:
      validate(plan) -> None (resolves only approved host-owned references);
      launch(plan) -> runtime_id; inspect(plan, runtime_id) ->
      {identity_ok, running, stopped, exit_code, runtime_id};
      stop(plan, runtime_id) -> None. The adapter must verify exact ownership on
      every observation and mutation. Unavailable evidence raises, never guesses.
    """

    def __init__(self, state_root: str | Path, runtime, *, clock=time.time):
        self.root = Path(state_root).absolute()
        if (not self.root.is_dir() or self.root.is_symlink() or
                self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077):
            raise ValueError("supervisor state root must be caller-owned and private")
        self.path = self.root / "supervisor.sqlite3"
        if self.path.is_symlink():
            raise ValueError("supervisor database symlink refused")
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077:
            raise ValueError("supervisor database must be private")
        self.runtime = runtime
        self.clock = clock
        try:
            with self._db() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    db.executescript(SCHEMA)
                    db.execute("INSERT INTO meta VALUES(?,?,?,?,?,?,?)", (
                        1, uuid.uuid4().hex, 0, None, 0, self.clock(), 0))
                elif version != 1:
                    raise Unavailable("unsupported supervisor schema")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise Unavailable("supervisor journal failed integrity check")
        except sqlite3.Error as exc:
            raise Unavailable("supervisor journal unavailable") from exc

    @contextmanager
    def _db(self):
        try:
            db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=1000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            if "db" in locals():
                db.close()

    @contextmanager
    def _tx(self):
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except sqlite3.Error as exc:
            raise Unavailable("supervisor journal unavailable; runtime outcome may be unknown") from exc

    def _clock_check(self, db):
        now = self.clock()
        meta = db.execute("SELECT * FROM meta").fetchone()
        if now < meta["last_wall"]:
            db.execute("UPDATE meta SET blocked=1")
            meta = {**dict(meta), "blocked": 1}
        db.execute("UPDATE meta SET last_wall=?", (now,))
        return now, meta

    @_serialized
    def acquire(self, controller: str, *, lease_seconds: int = 15, owner_takeover=False):
        if not controller or not 1 <= lease_seconds <= 60:
            raise ValueError("invalid control lease")
        with self._tx() as db:
            now, meta = self._clock_check(db)
            if meta["blocked"]:
                raise OwnershipUnknown("supervisor blocked")
            if meta["controller"] == controller and meta["lease_until"] > now:
                return {"supervisor_id": meta["supervisor_id"], "generation": meta["generation"]}
            if meta["lease_until"] > now and not owner_takeover:
                raise Fenced("live controller lease cannot be stolen")
            generation = meta["generation"] + 1
            db.execute("UPDATE meta SET controller=?,generation=?,lease_until=?", (
                controller, generation, now + lease_seconds))
            return {"supervisor_id": meta["supervisor_id"], "generation": generation}

    @_serialized
    def renew(self, controller: str, generation: int, *, lease_seconds=15):
        if not 1 <= lease_seconds <= 60:
            raise ValueError("invalid control lease")
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            db.execute("UPDATE meta SET lease_until=?", (now + lease_seconds,))
            # A timely renewal extends only attempts controlled by this same
            # generation. A later reacquisition cannot reset an orphan budget.
            db.execute("UPDATE attempts SET orphan_deadline=MIN(deadline,?+grace) "
                       "WHERE generation=? AND policy='strict' AND state!='stopped'", (
                           now + lease_seconds, generation))
            return now + lease_seconds

    @staticmethod
    def _authority(meta, controller, generation, now):
        if (meta["blocked"] or meta["controller"] != controller or
                meta["generation"] != generation or meta["lease_until"] <= now):
            raise Fenced("stale or expired controller")

    @_serialized
    def launch(self, plan: dict, *, controller: str, generation: int, operation_id: str):
        """Journal intent before runtime launch; same operation never launches twice."""
        required = {"job_id", "attempt_id", "incarnation", "worker_type", "profile_ref",
                    "workspace_ref", "payload", "deadline_seconds", "orphan_policy"}
        if set(plan) != required or not operation_id:
            raise ValueError("invalid resolved launch plan")
        if any(not isinstance(plan[key], str) or not plan[key] for key in
               ("job_id", "attempt_id", "incarnation", "worker_type", "profile_ref", "workspace_ref")):
            raise ValueError("invalid symbolic launch reference")
        if not isinstance(plan["payload"], dict) or len(_json(plan["payload"]).encode()) > 65_536:
            raise ValueError("invalid or oversized payload")
        if not 1 <= plan["deadline_seconds"] <= 86_400:
            raise ValueError("invalid deadline")
        policy = plan["orphan_policy"]
        if set(policy) != {"mode", "grace_seconds", "max_orphan_seconds"} or policy["mode"] not in {"strict", "trusted_local"}:
            raise ValueError("invalid orphan policy")
        if policy["mode"] == "trusted_local" and not 1 <= policy["max_orphan_seconds"] <= 86_400:
            raise ValueError("invalid orphan budget")
        if policy["mode"] == "strict" and policy["max_orphan_seconds"]:
            raise ValueError("strict orphan policy cannot continue")
        self.runtime.validate(plan)
        digest = hashlib.sha256(_json(plan).encode()).hexdigest()
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            old = db.execute("SELECT * FROM attempts WHERE id=?", (plan["attempt_id"],)).fetchone()
            if old:
                if old["plan_hash"] != digest or old["launch_op"] != operation_id:
                    raise Conflict("attempt or operation identity conflict")
                # A lost launch response must be reconciled, never repeated.
                return self._attempt(old)
            deadline = now + plan["deadline_seconds"]
            orphan_deadline = (min(deadline, now + policy["max_orphan_seconds"])
                               if policy["mode"] == "trusted_local"
                               else min(deadline, meta["lease_until"] + policy["grace_seconds"]))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                plan["attempt_id"], plan["job_id"], plan["incarnation"], digest, _json(plan),
                generation, operation_id, None, "launch_pending", 0, deadline,
                orphan_deadline, policy["mode"], policy["grace_seconds"], 0))
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                operation_id, plan["attempt_id"], "launch", "pending", None))
        # Recheck fencing at the actual mutation boundary, after durable intent.
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            item = db.execute("SELECT * FROM attempts WHERE id=?", (plan["attempt_id"],)).fetchone()
            if item["cancel"]:
                return self._attempt(item)
            db.execute("UPDATE attempts SET state='launch_calling' WHERE id=?", (plan["attempt_id"],))
        try:
            runtime_id = self.runtime.launch(plan)
        except Exception:
            # Runtime might have created a resource before the response was lost.
            with self._tx() as db:
                db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (plan["attempt_id"],))
            raise OwnershipUnknown("launch response unknown; reconcile exact attempt") from None
        with self._tx() as db:
            item = db.execute("SELECT * FROM attempts WHERE id=?", (plan["attempt_id"],)).fetchone()
            db.execute("UPDATE attempts SET runtime_id=?,state='running' WHERE id=?", (
                runtime_id, plan["attempt_id"]))
            db.execute("UPDATE operations SET status='confirmed',response=? WHERE id=?", (
                _json({"runtime_id": runtime_id}), operation_id))
            cancel = bool(item["cancel"])
        if cancel:
            self._stop_exact(plan["attempt_id"])
        return self.inspect(plan["attempt_id"])

    @staticmethod
    def _attempt(row):
        result = dict(row)
        result["plan"] = json.loads(result["plan"])
        return result

    def inspect(self, attempt_id: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise KeyError(attempt_id)
            return self._attempt(row)

    @_serialized
    def reconcile(self, attempt_id: str):
        """Inspect exact owned resource after restart; never create or adopt by label alone."""
        item = self.inspect(attempt_id)
        try:
            observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            if not observed["identity_ok"] or (item["runtime_id"] and observed["runtime_id"] != item["runtime_id"]):
                raise OwnershipUnknown("runtime ownership mismatch")
        except Exception:
            with self._tx() as db:
                db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (attempt_id,))
            raise OwnershipUnknown("runtime observation unavailable or mismatched") from None
        with self._tx() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row["runtime_id"] and row["runtime_id"] != observed["runtime_id"]:
                raise OwnershipUnknown("runtime identity changed")
            state = "stopped" if observed["stopped"] else "running" if observed["running"] else "unknown"
            db.execute("UPDATE attempts SET runtime_id=?,state=?,observation_seq=observation_seq+1 WHERE id=?", (
                observed["runtime_id"], state, attempt_id))
            cancelled = bool(row["cancel"])
        if cancelled and state != "stopped":
            return self._stop_exact(attempt_id)
        return self.inspect(attempt_id)

    @_serialized
    def cancel(self, attempt_id: str, *, controller: str, generation: int, operation_id: str):
        if not operation_id:
            raise ValueError("operation identity required")
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise KeyError(attempt_id)
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old and (old["attempt_id"] != attempt_id or old["kind"] != "cancel"):
                raise Conflict("operation identity conflict")
            if not old:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (operation_id, attempt_id, "cancel", "pending", None))
            db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (attempt_id,))
        return self._stop_exact(attempt_id)

    @_serialized
    def owner_stop(self, attempt_id: str, *, operation_id: str):
        """Owner API boundary: caller authentication is required by the local service."""
        if not operation_id:
            raise ValueError("operation identity required")
        with self._tx() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise KeyError(attempt_id)
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old and (old["attempt_id"] != attempt_id or old["kind"] != "owner_stop"):
                raise Conflict("operation identity conflict")
            if old:
                # Repeat the exact stop if its confirmation was lost, but do
                # not revoke a controller which acquired control afterwards.
                already_recorded = True
            else:
                already_recorded = False
            if row["state"] == "stopped":
                if not old:
                    db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                        operation_id, attempt_id, "owner_stop", "confirmed",
                        _json({"state": "stopped"})))
                else:
                    db.execute("UPDATE operations SET status='confirmed',response=? WHERE id=?", (
                        _json({"state": "stopped"}), operation_id))
                return self._attempt(row)
            if not old:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (operation_id, attempt_id, "owner_stop", "pending", None))
            db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (attempt_id,))
            if not already_recorded:
                db.execute("UPDATE meta SET generation=generation+1,controller=NULL,lease_until=0")
        return self._stop_exact(attempt_id)

    def _stop_exact(self, attempt_id):
        item = self.inspect(attempt_id)
        if item["state"] == "stopped":
            return item
        if item["state"] in {"launch_pending", "launch_calling"}:
            return item  # launch completion or reconciliation will honor sticky cancel
        try:
            observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            if not observed["identity_ok"] or observed["runtime_id"] != item["runtime_id"]:
                raise OwnershipUnknown("runtime ownership mismatch")
            if not observed["stopped"]:
                self.runtime.stop(item["plan"], item["runtime_id"])
                observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            if not observed["identity_ok"] or not observed["stopped"]:
                raise OwnershipUnknown("stop confirmation unavailable")
        except Exception:
            with self._tx() as db:
                db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (attempt_id,))
            raise OwnershipUnknown("exact stop not confirmed") from None
        with self._tx() as db:
            db.execute("UPDATE attempts SET state='stopped',observation_seq=observation_seq+1 WHERE id=?", (attempt_id,))
            db.execute("UPDATE operations SET status='confirmed',response=? WHERE attempt_id=? AND kind IN ('cancel','owner_stop')", (
                _json({"state": "stopped"}), attempt_id))
        return self.inspect(attempt_id)

    @_serialized
    def tick(self):
        """Supervisor-owned watchdog; call periodically regardless of coordinator state."""
        with self._tx() as db:
            now, meta = self._clock_check(db)
            rows = db.execute("SELECT * FROM attempts WHERE state!='stopped'").fetchall()
            due = []
            for row in rows:
                stop_at = row["deadline"]
                if row["generation"] != meta["generation"] or meta["lease_until"] <= now:
                    stop_at = min(stop_at, row["orphan_deadline"])
                if meta["blocked"] or now >= stop_at or row["cancel"]:
                    db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (row["id"],))
                    due.append(row["id"])
        for attempt_id in due:
            self._stop_exact(attempt_id)
        return due
