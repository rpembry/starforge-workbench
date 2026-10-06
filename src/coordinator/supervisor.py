"""Independent host ownership journal and fenced runtime boundary.

The runtime adapter is host-owned. Its inspect/stop methods MUST target only an
exact, previously verified owned attempt; supervisor never accepts a Docker ID,
path, image, or mount from a client. This module does not contact Workbench.
"""

from contextlib import contextmanager
import base64
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


class WatchdogUncertain(OwnershipUnknown):
    def __init__(self, stopped, uncertain):
        self.stopped = stopped
        self.uncertain = uncertain
        confirmed = set(stopped)
        self.readiness_uncertain = [item for item in uncertain if item in confirmed]
        self.runtime_uncertain = [item for item in uncertain if item not in confirmed]
        if self.runtime_uncertain:
            message = "watchdog runtime observation or exact stop uncertain"
        else:
            message = "readiness observation uncertain; exact stop confirmed"
        super().__init__(message)


class RecoveryUncertain(OwnershipUnknown):
    def __init__(self, verified, uncertain):
        self.verified = verified
        self.uncertain = uncertain
        super().__init__("startup ownership recovery incomplete; new starts blocked")


class LaunchRejected(ValueError):
    """The new plan was rejected before a launch intent was journaled."""


class PlanRejected(ValueError):
    """The runtime explicitly rejected a deterministic plan property."""


class ReservationFull(Conflict):
    """A valid plan is waiting for supervisor host capacity."""


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
 blocked INTEGER NOT NULL DEFAULT 0, recovery_required INTEGER NOT NULL DEFAULT 0);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, incarnation TEXT NOT NULL,
 plan_hash TEXT NOT NULL, plan TEXT NOT NULL, generation INTEGER NOT NULL,
 launch_op TEXT NOT NULL UNIQUE, runtime_id TEXT, state TEXT NOT NULL,
 cancel INTEGER NOT NULL DEFAULT 0, deadline REAL NOT NULL,
 orphan_deadline REAL NOT NULL, policy TEXT NOT NULL, grace REAL NOT NULL,
 observation_seq INTEGER NOT NULL DEFAULT 0,
 reserved_cpu_millis INTEGER, reserved_memory_mb INTEGER,
 no_start_reason TEXT);
CREATE TABLE operations(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
 kind TEXT NOT NULL, status TEXT NOT NULL, response TEXT);
CREATE TABLE evidence(attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), response TEXT NOT NULL);
PRAGMA user_version=5;
COMMIT;
"""

MIGRATE_1_TO_2 = """
BEGIN IMMEDIATE;
CREATE TABLE evidence(attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), response TEXT NOT NULL);
PRAGMA user_version=2;
COMMIT;
"""

MIGRATE_2_TO_3 = """
BEGIN IMMEDIATE;
ALTER TABLE meta ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version=3;
COMMIT;
"""

MIGRATE_3_TO_4 = """
BEGIN IMMEDIATE;
ALTER TABLE attempts ADD COLUMN reserved_cpu_millis INTEGER;
ALTER TABLE attempts ADD COLUMN reserved_memory_mb INTEGER;
PRAGMA user_version=4;
COMMIT;
"""

MIGRATE_4_TO_5 = """
BEGIN IMMEDIATE;
ALTER TABLE attempts ADD COLUMN no_start_reason TEXT;
UPDATE attempts SET no_start_reason='prelaunch_abandon'
 WHERE state='stopped' AND runtime_id IS NULL AND cancel=1;
PRAGMA user_version=5;
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
                    db.execute("INSERT INTO meta VALUES(?,?,?,?,?,?,?,?)", (
                        1, uuid.uuid4().hex, 0, None, 0, self.clock(), 0, 0))
                elif version == 1:
                    db.executescript(MIGRATE_1_TO_2)
                    db.executescript(MIGRATE_2_TO_3)
                    db.executescript(MIGRATE_3_TO_4)
                    db.executescript(MIGRATE_4_TO_5)
                elif version == 2:
                    db.executescript(MIGRATE_2_TO_3)
                    db.executescript(MIGRATE_3_TO_4)
                    db.executescript(MIGRATE_4_TO_5)
                elif version == 3:
                    db.executescript(MIGRATE_3_TO_4)
                    db.executescript(MIGRATE_4_TO_5)
                elif version == 4:
                    db.executescript(MIGRATE_4_TO_5)
                elif version != 5:
                    raise Unavailable("unsupported supervisor schema")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise Unavailable("supervisor journal failed integrity check")
                if db.execute("SELECT 1 FROM attempts WHERE state!='stopped' LIMIT 1").fetchone():
                    db.execute("UPDATE meta SET recovery_required=1")
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

    def _validated_plan(self, plan, operation_id):
        required = {"job_id", "attempt_id", "incarnation", "worker_type", "profile_ref",
                    "workspace_ref", "payload", "deadline_seconds", "orphan_policy"}
        if set(plan) != required or not operation_id:
            raise ValueError("invalid resolved launch plan")
        if any(not isinstance(plan[key], str) or not plan[key] for key in
               ("job_id", "attempt_id", "incarnation", "worker_type", "profile_ref", "workspace_ref")):
            raise ValueError("invalid symbolic launch reference")
        if not isinstance(plan["payload"], dict) or len(_json(plan["payload"]).encode()) > 65_536:
            raise ValueError("invalid or oversized payload")
        if type(plan["deadline_seconds"]) is not int or not 1 <= plan["deadline_seconds"] <= 86_400:
            raise ValueError("invalid deadline")
        policy = plan["orphan_policy"]
        if set(policy) != {"mode", "grace_seconds", "max_orphan_seconds"} or policy["mode"] not in {"strict", "trusted_local"}:
            raise ValueError("invalid orphan policy")
        if type(policy["grace_seconds"]) is not int or not 0 <= policy["grace_seconds"] <= 300:
            raise ValueError("invalid orphan grace")
        if type(policy["max_orphan_seconds"]) is not int:
            raise ValueError("invalid orphan budget")
        if policy["mode"] == "trusted_local" and not 1 <= policy["max_orphan_seconds"] <= 86_400:
            raise ValueError("invalid orphan budget")
        if policy["mode"] == "strict" and policy["max_orphan_seconds"]:
            raise ValueError("strict orphan policy cannot continue")
        return hashlib.sha256(_json(plan).encode()).hexdigest()

    @_serialized
    def launch(self, plan: dict, *, controller: str, generation: int, operation_id: str):
        """Journal intent before runtime launch; same operation never launches twice."""
        digest = self._validated_plan(plan, operation_id)
        policy = plan["orphan_policy"]
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            if meta["recovery_required"]:
                raise RecoveryUncertain([], ["startup_reconciliation_required"])
            old = db.execute("SELECT * FROM attempts WHERE id=?", (plan["attempt_id"],)).fetchone()
            if old:
                if old["plan_hash"] != digest or old["launch_op"] != operation_id:
                    raise Conflict("attempt or operation identity conflict")
                # A lost launch response must be reconciled, never repeated.
                return self._attempt(old)
            try:
                self.runtime.validate(plan)  # current policy gates only NEW runtime allocations
            except Conflict:
                raise
            except PlanRejected as exc:
                raise LaunchRejected("runtime plan rejected before launch") from exc
            reservation = getattr(self.runtime, "reservation", None)
            host_budget = getattr(self.runtime, "host_budget", None)
            requested = reservation(plan) if reservation is not None else None
            if reservation is not None and host_budget is not None:
                active = db.execute("SELECT reserved_cpu_millis,reserved_memory_mb FROM attempts WHERE state!='stopped'").fetchall()
                used_cpu = used_memory = 0
                for item in active:
                    if item["reserved_cpu_millis"] is None or item["reserved_memory_mb"] is None:
                        raise Conflict("existing allocation reservation unknown")
                    used_cpu += item["reserved_cpu_millis"]
                    used_memory += item["reserved_memory_mb"]
                if (len(active) >= host_budget["max_active"] or
                        used_cpu + requested["cpu_millis"] > host_budget["cpu_millis"] or
                        used_memory + requested["memory_mb"] > host_budget["memory_mb"]):
                    raise ReservationFull("supervisor host reservation full")
            deadline = now + plan["deadline_seconds"]
            orphan_deadline = (min(deadline, now + policy["max_orphan_seconds"])
                               if policy["mode"] == "trusted_local"
                               else min(deadline, meta["lease_until"] + policy["grace_seconds"]))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                plan["attempt_id"], plan["job_id"], plan["incarnation"], digest, _json(plan),
                generation, operation_id, None, "launch_pending", 0, deadline,
                orphan_deadline, policy["mode"], policy["grace_seconds"], 0,
                requested["cpu_millis"] if requested else None,
                requested["memory_mb"] if requested else None, None))
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                operation_id, plan["attempt_id"], "launch", "pending", None))
        # Recheck fencing at the actual mutation boundary, after durable intent.
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            if meta["recovery_required"]:
                raise RecoveryUncertain([], ["startup_reconciliation_required"])
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

    @_serialized
    def abandon(self, plan: dict, *, controller: str, generation: int, operation_id: str):
        """Fence a durable launch intent before create; never start to cancel."""
        digest = self._validated_plan(plan, operation_id)
        policy = plan["orphan_policy"]
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            old = db.execute("SELECT * FROM attempts WHERE id=?", (plan["attempt_id"],)).fetchone()
            if old:
                if old["plan_hash"] != digest or old["launch_op"] != operation_id:
                    raise Conflict("attempt or operation identity conflict")
                if old["state"] == "stopped":
                    return self._attempt(old)
                db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (plan["attempt_id"],))
                if old["state"] == "launch_pending":
                    db.execute("UPDATE attempts SET state='stopped',no_start_reason='prelaunch_abandon',observation_seq=observation_seq+1 WHERE id=?", (
                        plan["attempt_id"],))
                    db.execute("UPDATE operations SET status='confirmed',response=? WHERE id=?", (
                        _json({"state": "stopped", "no_start": True,
                               "no_start_reason": "prelaunch_abandon"}), operation_id))
                    return self._attempt(db.execute("SELECT * FROM attempts WHERE id=?", (
                        plan["attempt_id"],)).fetchone())
            else:
                deadline = now + plan["deadline_seconds"]
                orphan_deadline = (min(deadline, now + policy["max_orphan_seconds"])
                                   if policy["mode"] == "trusted_local"
                                   else min(deadline, meta["lease_until"] + policy["grace_seconds"]))
                db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    plan["attempt_id"], plan["job_id"], plan["incarnation"], digest, _json(plan),
                    generation, operation_id, None, "stopped", 1, deadline,
                    orphan_deadline, policy["mode"], policy["grace_seconds"], 1,
                    None, None, "prelaunch_abandon"))
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                    operation_id, plan["attempt_id"], "launch", "confirmed",
                    _json({"state": "stopped", "no_start": True,
                           "no_start_reason": "prelaunch_abandon"})))
                return self._attempt(db.execute("SELECT * FROM attempts WHERE id=?", (
                    plan["attempt_id"],)).fetchone())
        # Existing create/start may have happened. Stop only exact owned runtime.
        return self._stop_exact(plan["attempt_id"])

    @staticmethod
    def _attempt(row):
        result = dict(row)
        result["plan"] = json.loads(result["plan"])
        return result

    def inspect(self, attempt_id: str):
        try:
            with self._db() as db:
                row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                if not row:
                    raise KeyError(attempt_id)
                return self._attempt(row)
        except sqlite3.Error as exc:
            raise Unavailable("supervisor journal read unavailable") from exc

    @_serialized
    def collect(self, attempt_id: str, *, controller: str, generation: int):
        """Return durable positive runtime and artifact evidence, never a bare claim."""
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
        with self._db() as db:
            saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (attempt_id,)).fetchone()
            if saved:
                return json.loads(saved["response"])
        attempt = self.inspect(attempt_id)
        if attempt["state"] == "stopped" and attempt["no_start_reason"]:
            reason = attempt["no_start_reason"]
            if reason == "workspace_setup_failed":
                probe = getattr(self.runtime, "inspect_no_start", None)
                try:
                    confirmed = probe is not None and probe(attempt["plan"])
                except Exception:
                    confirmed = False
                if not confirmed:
                    raise OwnershipUnknown("precreate absence evidence unavailable")
            with self._tx() as db:
                now, meta = self._clock_check(db)
                self._authority(meta, controller, generation, now)
                saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (attempt_id,)).fetchone()
                if saved:
                    return json.loads(saved["response"])
                row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                if (row["state"] != "stopped" or row["runtime_id"] is not None or
                        row["no_start_reason"] != reason):
                    raise OwnershipUnknown("no-start evidence changed")
                db.execute("UPDATE attempts SET observation_seq=observation_seq+1 WHERE id=?", (attempt_id,))
                response = {"supervisor_id": meta["supervisor_id"], "job_id": row["job_id"],
                            "attempt_id": attempt_id, "incarnation": row["incarnation"],
                            "runtime_id": None, "observation_seq": row["observation_seq"] + 1,
                            "phase": "stopped", "stopped": True, "exit_code": None,
                            "result_ok": False, "no_start": True, "no_start_reason": reason}
                db.execute("INSERT INTO evidence VALUES(?,?)", (attempt_id, _json(response)))
                return response
        if attempt["state"] != "stopped" or not attempt["runtime_id"]:
            raise OwnershipUnknown("attempt has no confirmed stopped runtime to collect")
        observed = self.runtime.inspect(attempt["plan"], attempt["runtime_id"])
        if (not observed["identity_ok"] or not observed["stopped"] or
                observed["runtime_id"] != attempt["runtime_id"]):
            raise OwnershipUnknown("positive stopped runtime evidence unavailable")
        manifest_path = Path(self.runtime.collect(attempt["plan"], attempt["runtime_id"]))
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 131_072:
            raise OwnershipUnknown("artifact manifest unavailable or oversized")
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get("attempt_id") != attempt_id or
                not any(manifest.get("execution_ok") is value for value in (True, False, None))):
            raise OwnershipUnknown("artifact manifest identity mismatch")
        execution_ok = manifest["execution_ok"]
        if execution_ok is True and observed["exit_code"] != 0:
            raise OwnershipUnknown("result conflicts with process exit")
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
            saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (attempt_id,)).fetchone()
            if saved:
                return json.loads(saved["response"])
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row["state"] != "stopped" or row["runtime_id"] != observed["runtime_id"]:
                raise OwnershipUnknown("attempt changed during collection")
            db.execute("UPDATE attempts SET observation_seq=observation_seq+1 WHERE id=?", (attempt_id,))
            seq = row["observation_seq"] + 1
            response = {"supervisor_id": db.execute("SELECT supervisor_id FROM meta").fetchone()[0],
                        "job_id": row["job_id"], "attempt_id": attempt_id,
                        "incarnation": row["incarnation"], "runtime_id": row["runtime_id"],
                        "observation_seq": seq, "phase": "stopped", "stopped": True,
                        "exit_code": observed["exit_code"], "result_ok": execution_ok,
                        "artifact_manifest": manifest}
            db.execute("INSERT INTO evidence VALUES(?,?)", (attempt_id, _json(response)))
            return response

    def read_artifact(self, attempt_id: str, name: str, *, offset=0, limit=65_536):
        if not isinstance(name, str) or not 0 <= offset or not 1 <= limit <= 65_536:
            raise ValueError("invalid artifact read")
        with self._db() as db:
            saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not saved:
                raise OwnershipUnknown("artifact evidence not committed")
            evidence = json.loads(saved["response"])
        if evidence.get("no_start"):
            raise ValueError("never-started attempt has no artifacts")
        entry = next((item for item in evidence["artifact_manifest"]["files"]
                      if item["path"] == name), None)
        if entry is None:
            raise ValueError("artifact not declared")
        attempt = self.inspect(attempt_id)
        data = self.runtime.read_artifact(attempt["plan"], attempt["runtime_id"], name)
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise OwnershipUnknown("artifact changed after evidence commit")
        chunk = data[offset:offset + limit]
        return {"attempt_id": attempt_id, "name": name, "offset": offset,
                "next_offset": offset + len(chunk), "total": len(data),
                "sha256": entry["sha256"], "data_base64": base64.b64encode(chunk).decode()}

    def _collected_stopped(self, attempt_id):
        attempt = self.inspect(attempt_id)
        if attempt["state"] != "stopped" or not attempt["runtime_id"]:
            raise OwnershipUnknown("confirmed stopped runtime required")
        with self._db() as db:
            saved = db.execute("SELECT 1 FROM evidence WHERE attempt_id=?", (attempt_id,)).fetchone()
        if not saved:
            raise OwnershipUnknown("committed artifact and exit evidence required")
        return attempt

    def _collected_no_start(self, attempt):
        if (attempt["state"] != "stopped" or attempt["runtime_id"] is not None or
                attempt["no_start_reason"] != "workspace_setup_failed"):
            raise OwnershipUnknown("confirmed workspace no-start required")
        with self._db() as db:
            saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (
                attempt["id"],)).fetchone()
        if not saved:
            raise OwnershipUnknown("committed no-start evidence required")
        evidence = json.loads(saved["response"])
        if (evidence.get("attempt_id") != attempt["id"] or
                evidence.get("job_id") != attempt["job_id"] or
                evidence.get("incarnation") != attempt["incarnation"] or
                evidence.get("runtime_id", False) is not None or
                evidence.get("exit_code", False) is not None or
                evidence.get("no_start") is not True or
                evidence.get("no_start_reason") != "workspace_setup_failed" or
                evidence.get("stopped") is not True):
            raise OwnershipUnknown("no-start evidence identity changed")
        return attempt

    def _archive_absence_proven(self, item):
        """Require original collection and matching durable archive intent."""
        if not item["runtime_id"]:
            return False
        probe = getattr(self.runtime, "inspect_archive_absent", None)
        if probe is None:
            return False
        with self._db() as db:
            saved = db.execute("SELECT response FROM evidence WHERE attempt_id=?", (
                item["id"],)).fetchone()
            operations = db.execute("SELECT * FROM operations WHERE attempt_id=? AND kind='archive'", (
                item["id"],)).fetchall()
        if not saved:
            return False
        evidence = json.loads(saved["response"])
        if (evidence.get("runtime_id") != item["runtime_id"] or
                evidence.get("attempt_id") != item["id"] or
                evidence.get("incarnation") != item["incarnation"] or
                not evidence.get("stopped") or evidence.get("no_start")):
            return False
        for operation in operations:
            try:
                digest = json.loads(operation["response"])["review_sha256"]
                if probe(item["plan"], item["runtime_id"], digest, operation["id"]):
                    return True
            except Exception:
                continue
        return False

    def _restore_archiving_stop(self, item):
        if item["state"] != "unknown" or not self._archive_absence_proven(item):
            return item
        with self._tx() as db:
            db.execute("UPDATE attempts SET state='stopped',observation_seq=observation_seq+1 "
                       "WHERE id=? AND state='unknown' AND runtime_id=?", (
                           item["id"], item["runtime_id"]))
        return self.inspect(item["id"])

    @_serialized
    def review(self, attempt_id: str):
        """Owner-only immutable review snapshot before any archive mutation."""
        item = self.inspect(attempt_id)
        if item["no_start_reason"]:
            item = self._collected_no_start(item)
            review = getattr(self.runtime, "review_no_start", None)
            if review is None:
                raise OwnershipUnknown("no-start retention adapter unavailable")
            return review(item["plan"])
        attempt = self._collected_stopped(attempt_id)
        return self.runtime.review(attempt["plan"], attempt["runtime_id"])

    @_serialized
    def archive(self, attempt_id: str, *, review_sha256: str, operation_id: str):
        """Archive only exact reviewed bytes, retaining the original content."""
        if (not operation_id or not isinstance(review_sha256, str) or
                len(review_sha256) != 64 or any(c not in "0123456789abcdef" for c in review_sha256)):
            raise ValueError("review digest and operation identity required")
        item = self.inspect(attempt_id)
        if item["no_start_reason"]:
            return self._retain_no_start(item, review_sha256, operation_id)
        self._restore_archiving_stop(self.inspect(attempt_id))
        attempt = self._collected_stopped(attempt_id)
        confirmed_response = None
        with self._tx() as db:
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if (old["attempt_id"] != attempt_id or old["kind"] != "archive" or
                        json.loads(old["response"])["review_sha256"] != review_sha256):
                    raise Conflict("archive operation identity changed")
                if old["status"] == "confirmed":
                    confirmed_response = json.loads(old["response"])
            else:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                    operation_id, attempt_id, "archive", "pending",
                    _json({"review_sha256": review_sha256})))
        if confirmed_response is not None:
            self.runtime.archive(attempt["plan"], attempt["runtime_id"],
                                 review_sha256, operation_id)
            return confirmed_response
        result = self.runtime.archive(attempt["plan"], attempt["runtime_id"],
                                      review_sha256, operation_id)
        response = {"attempt_id": attempt_id, "review_sha256": review_sha256,
                    "phase": result["phase"], "archive": result["archive"]}
        with self._tx() as db:
            db.execute("UPDATE operations SET status='confirmed',response=? WHERE id=?", (
                _json(response), operation_id))
        return response

    def _retain_no_start(self, item, review_sha256, operation_id):
        item = self._collected_no_start(item)
        retain = getattr(self.runtime, "retain_no_start", None)
        if retain is None:
            raise OwnershipUnknown("no-start retention adapter unavailable")
        with self._tx() as db:
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if (old["attempt_id"] != item["id"] or old["kind"] != "no_start_retention" or
                        json.loads(old["response"])["review_sha256"] != review_sha256):
                    raise Conflict("retention operation identity changed")
            else:
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                    operation_id, item["id"], "no_start_retention", "pending",
                    _json({"review_sha256": review_sha256})))
        result = retain(item["plan"], review_sha256, operation_id)
        response = {"attempt_id": item["id"], "evidence_kind": "no_start_retention",
                    "review_sha256": review_sha256, "phase": result["phase"],
                    "archive": result["archive"]}
        with self._tx() as db:
            db.execute("UPDATE operations SET status='confirmed',response=? WHERE id=?", (
                _json(response), operation_id))
        return response

    def recovery_required(self):
        try:
            with self._db() as db:
                return bool(db.execute("SELECT recovery_required FROM meta").fetchone()[0])
        except sqlite3.Error as exc:
            raise Unavailable("supervisor recovery state unavailable") from exc

    @_serialized
    def recover_startup(self):
        """Service-owned exact inspection gate before new starts are allowed."""
        with self._tx() as db:
            db.execute("UPDATE meta SET recovery_required=1")
            ids = [row[0] for row in db.execute(
                "SELECT id FROM attempts WHERE state!='stopped' ORDER BY rowid")]
        verified, uncertain = [], []
        for attempt_id in ids:
            try:
                outcome = self._reconcile_owned(attempt_id)
                if outcome["state"] in {"running", "stopped"}:
                    verified.append(attempt_id)
                else:
                    uncertain.append(attempt_id)
            except (OwnershipUnknown, Unavailable):
                uncertain.append(attempt_id)
        if uncertain:
            raise RecoveryUncertain(verified, uncertain)
        with self._tx() as db:
            db.execute("UPDATE meta SET recovery_required=0")
        return verified

    @_serialized
    def reconcile(self, attempt_id: str, *, controller: str, generation: int):
        """Current controller may reconcile; a stale caller cannot mutate state."""
        with self._tx() as db:
            now, meta = self._clock_check(db)
            self._authority(meta, controller, generation, now)
        return self._reconcile_owned(attempt_id)

    def _reconcile_owned(self, attempt_id: str):
        """Exact host inspection; only caller holding serialized authority uses it."""
        item = self.inspect(attempt_id)
        item = self._restore_archiving_stop(item)
        if item["state"] == "stopped" and item["no_start_reason"] and not item["runtime_id"]:
            return item  # durable prelaunch tombstone; no runtime to reattach
        if not item["runtime_id"]:
            probe = getattr(self.runtime, "inspect_no_start", None)
            try:
                proved_absent = probe is not None and probe(item["plan"])
            except Exception:
                proved_absent = False  # lost or ambiguous host evidence remains unknown below
            if proved_absent:
                return self._record_no_start(attempt_id, "workspace_setup_failed")
        if item["state"] == "stopped" and item["runtime_id"]:
            if self._archive_absence_proven(item):
                return item  # durable archive intent and exact absence; replay owns completion
            archived = getattr(self.runtime, "inspect_archived", None)
            if archived is not None and archived(item["plan"], item["runtime_id"]):
                return item  # exact committed archive, not a missing live runtime
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

    def _record_no_start(self, attempt_id, reason):
        """Persist positive absence before exposing a terminal no-start result."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row["runtime_id"] is not None:
                raise OwnershipUnknown("runtime identity already saved")
            if row["state"] == "stopped" and row["no_start_reason"] == reason:
                return self._attempt(row)
            if row["state"] == "stopped":
                raise OwnershipUnknown("stopped attempt has different evidence")
            db.execute("UPDATE attempts SET state='stopped',no_start_reason=?,"
                       "observation_seq=observation_seq+1 WHERE id=?", (reason, attempt_id))
            response = _json({"state": "stopped", "no_start": True,
                              "no_start_reason": reason})
            db.execute("UPDATE operations SET status='confirmed',response=? "
                       "WHERE attempt_id=? AND kind IN ('launch','cancel','owner_stop')", (
                           response, attempt_id))
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
        try:
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
                    db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
                        operation_id, attempt_id, "owner_stop", "pending", None))
                db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (attempt_id,))
                if not already_recorded:
                    db.execute("UPDATE meta SET generation=generation+1,controller=NULL,lease_until=0")
        except Unavailable:
            self._emergency_stop_without_journal(attempt_id)
            raise OwnershipUnknown("owner stop outcome uncertain; journal fencing unavailable") from None
        return self._stop_exact(attempt_id)

    def _emergency_stop_without_journal(self, attempt_id):
        """Best effort only: no durable fence or confirmation can be claimed.

        The caller still holds the cross-process lock. A persisted runtime ID
        and positive adapter ownership evidence are mandatory; a launch with
        no saved ID must be left alone because its create outcome is unknown.
        """
        try:
            item = self.inspect(attempt_id)
            if item["state"] not in {"running", "unknown"} or not item["runtime_id"]:
                return
            observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            if (not observed["identity_ok"] or
                    observed["runtime_id"] != item["runtime_id"]):
                return
            if not observed["stopped"]:
                self.runtime.stop(item["plan"], item["runtime_id"])
                observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            # Even a positive stopped observation cannot be reported as a
            # confirmed owner stop without a committed cancel and fence.
        except Exception:
            return

    def _stop_exact(self, attempt_id):
        item = self.inspect(attempt_id)
        if item["state"] == "stopped":
            return item
        if item["state"] == "launch_pending" and item["cancel"]:
            # Runtime launch is entered only after a durable transition to
            # launch_calling under this same cross-process control lock.
            with self._tx() as db:
                db.execute("UPDATE attempts SET state='stopped',no_start_reason='prelaunch_abandon',observation_seq=observation_seq+1 WHERE id=? AND state='launch_pending'", (
                    attempt_id,))
                db.execute("UPDATE operations SET status='confirmed',response=? WHERE attempt_id=? AND kind IN ('cancel','owner_stop')", (
                    _json({"state": "stopped", "no_start": True,
                           "no_start_reason": "prelaunch_abandon"}), attempt_id))
                db.execute("UPDATE operations SET status='confirmed',response=? WHERE attempt_id=? AND kind='launch'", (
                    _json({"state": "stopped", "no_start": True,
                           "no_start_reason": "prelaunch_abandon"}), attempt_id))
            return self.inspect(attempt_id)
        if not item["runtime_id"]:
            probe = getattr(self.runtime, "inspect_no_start", None)
            try:
                proved_absent = probe is not None and probe(item["plan"])
            except Exception:
                proved_absent = False
            if proved_absent:
                return self._record_no_start(attempt_id, "workspace_setup_failed")
        if item["state"] == "launch_calling":
            return item  # unknown create outcome; reconciliation will honor sticky cancel
        try:
            observed = self.runtime.inspect(item["plan"], item["runtime_id"])
            if (not observed["identity_ok"] or not observed["runtime_id"] or
                    item["runtime_id"] and observed["runtime_id"] != item["runtime_id"]):
                raise OwnershipUnknown("runtime ownership mismatch")
            if item["runtime_id"] is None:
                # Lost create/start response: adopt only a resource that the
                # host adapter verified against the journal's exact plan,
                # token, labels, incarnation and receipt. Never guess an ID.
                with self._tx() as db:
                    db.execute("UPDATE attempts SET runtime_id=? WHERE id=? AND runtime_id IS NULL", (
                        observed["runtime_id"], attempt_id))
            if not observed["stopped"]:
                self.runtime.stop(item["plan"], observed["runtime_id"])
                observed = self.runtime.inspect(item["plan"], observed["runtime_id"])
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
        """Observe exits and enforce readiness/deadlines without coordinator liveness."""
        with self._tx() as db:
            now, meta = self._clock_check(db)
            rows = db.execute("SELECT * FROM attempts WHERE state!='stopped'").fetchall()
            due = []
            observe = []
            for row in rows:
                stop_at = row["deadline"]
                if row["generation"] != meta["generation"] or meta["lease_until"] <= now:
                    stop_at = min(stop_at, row["orphan_deadline"])
                if meta["blocked"] or now >= stop_at or row["cancel"]:
                    db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (row["id"],))
                    due.append(row["id"])
                elif row["state"] == "running":
                    observe.append((row["id"], row["deadline"], json.loads(row["plan"])))
        stopped, uncertain = [], []
        for attempt_id in due:
            try:
                result = self._stop_exact(attempt_id)
                if result["state"] == "stopped":
                    stopped.append(attempt_id)
                else:
                    uncertain.append(attempt_id)
            except (OwnershipUnknown, Unavailable):
                uncertain.append(attempt_id)
        for attempt_id, deadline, plan in observe:
            try:
                # Exact runtime observation also records normal exits while
                # the coordinator has no lease or is entirely absent.
                saved = self.inspect(attempt_id)
                observed = self.runtime.inspect(plan, saved["runtime_id"])
                if (not observed["identity_ok"] or
                        observed["runtime_id"] != saved["runtime_id"]):
                    raise OwnershipUnknown("runtime ownership mismatch")
                if observed["stopped"]:
                    self._reconcile_owned(attempt_id)
                    stopped.append(attempt_id)
                    continue
                if not observed["running"]:
                    uncertain.append(attempt_id)
                    continue
                if plan["worker_type"] == "protocol_example":
                    start_at = deadline - plan["deadline_seconds"]
                    ready = False
                    try:
                        ready = self.runtime.protocol_ready(plan, saved["runtime_id"])
                    except Exception:
                        # Failed inbox observation cannot extend the worker's
                        # readiness window after exact runtime verification.
                        uncertain.append(attempt_id)
                    if not ready and now >= start_at + min(30, plan["deadline_seconds"]):
                        with self._tx() as db:
                            db.execute("UPDATE attempts SET cancel=1 WHERE id=?", (attempt_id,))
                        outcome = self._stop_exact(attempt_id)
                        if outcome["state"] == "stopped":
                            stopped.append(attempt_id)
                        else:
                            uncertain.append(attempt_id)
            except Exception:
                uncertain.append(attempt_id)
        if uncertain:
            raise WatchdogUncertain(stopped, uncertain)
        return stopped
