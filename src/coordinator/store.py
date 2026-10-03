"""Durable job intent and evidence; no Workbench or Docker dependency.

Every externally meaningful mutation is one IMMEDIATE SQLite transaction. A caller
must reconcile an uncertain response by its idempotency key before doing more work.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .contracts import JobSpec, Policy


class Conflict(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE meta(version INTEGER NOT NULL CHECK(version=1));
INSERT INTO meta VALUES(1);
CREATE TABLE jobs(
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, version INTEGER NOT NULL,
 intent TEXT NOT NULL CHECK(intent IN ('run','cancel')),
 phase TEXT NOT NULL CHECK(phase IN ('queued','active','finalizing','terminal')),
 outcome TEXT, visibility TEXT NOT NULL CHECK(visibility IN ('unknown','fresh')),
 reason TEXT, seq INTEGER NOT NULL, attempt_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL,
 incarnation TEXT NOT NULL, phase TEXT NOT NULL, outcome TEXT, exit_code INTEGER,
 supervisor_id TEXT, runtime_id TEXT, observation_seq INTEGER NOT NULL DEFAULT 0,
 last_observed_at TEXT, stopped INTEGER NOT NULL DEFAULT 0,
 UNIQUE(job_id,ordinal));
CREATE TABLE operations(
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, key TEXT NOT NULL,
 kind TEXT NOT NULL, body_hash TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
 outcome TEXT NOT NULL CHECK(outcome IN ('pending','running','confirmed','unknown')),
 response TEXT NOT NULL, UNIQUE(principal,key));
CREATE TABLE operation_aliases(
 principal TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
 body_hash TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(id),
 PRIMARY KEY(principal,key));
CREATE TABLE events(
 job_id TEXT NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL, kind TEXT NOT NULL,
 data TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(job_id,seq));
CREATE TABLE outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, seq INTEGER NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0, UNIQUE(job_id,seq),
 FOREIGN KEY(job_id,seq) REFERENCES events(job_id,seq));
CREATE INDEX jobs_dispatch ON jobs(phase,intent,created_at);
COMMIT;
"""

MIGRATE_1_TO_2 = """
BEGIN IMMEDIATE;
CREATE TABLE operation_aliases(
 principal TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
 body_hash TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(id),
 PRIMARY KEY(principal,key));
PRAGMA user_version=2;
COMMIT;
"""


class CoordinatorStore:
    def __init__(self, state_root: str | Path, policy: Policy):
        self.root = Path(state_root).absolute()
        self.policy = policy
        if (not self.root.is_dir() or self.root.is_symlink() or
                self.root.stat().st_uid != os.getuid() or
                self.root.stat().st_mode & 0o077):
            raise ValueError("state root must be an existing caller-owned private directory")
        self.path = self.root / "coordinator.sqlite3"
        if self.path.is_symlink():
            raise ValueError("database symlink refused")
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077:
            raise ValueError("database must be caller-owned and private")
        try:
            with self._connection() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    # executescript is used only for first creation. Its explicit transaction
                    # keeps schema and version atomic even if creation is interrupted.
                    db.executescript(SCHEMA.replace("COMMIT;", "PRAGMA user_version=2;\nCOMMIT;"))
                elif version == 1:
                    db.executescript(MIGRATE_1_TO_2)
                elif version != 2:
                    raise Unavailable("unsupported coordinator schema")
                check = db.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise Unavailable("coordinator store failed integrity check")
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    @contextmanager
    def _connection(self):
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
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable; operation outcome may be unknown") from exc

    @staticmethod
    def _event(db, job_id, kind, data):
        db.execute("UPDATE jobs SET seq=seq+1, version=version+1, updated_at=? WHERE id=?", (_stamp(), job_id))
        seq = db.execute("SELECT seq FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (job_id, seq, kind, _json(data), _stamp()))
        db.execute("INSERT INTO outbox(job_id,seq) VALUES(?,?)", (job_id, seq))

    @staticmethod
    def _operation(db, principal, key, kind, body):
        if (not isinstance(principal, str) or not 1 <= len(principal) <= 128 or
                not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise ValueError("bounded principal and idempotency key required")
        digest = hashlib.sha256(_json(body).encode()).hexdigest()
        row = db.execute("SELECT * FROM operations WHERE principal=? AND key=?", (principal, key)).fetchone()
        if row:
            if row["kind"] != kind or row["body_hash"] != digest:
                raise Conflict("idempotency key reused for different command")
            return row, digest
        alias = db.execute("SELECT * FROM operation_aliases WHERE principal=? AND key=?", (
            principal, key)).fetchone()
        if alias:
            if alias["kind"] != kind or alias["body_hash"] != digest:
                raise Conflict("idempotency key reused for different command")
            return db.execute("SELECT * FROM operations WHERE id=?", (alias["operation_id"],)).fetchone(), digest
        return None, digest

    @staticmethod
    def _record_op(db, principal, key, kind, digest, job_id, outcome, response):
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (
            uuid.uuid4().hex, principal, key, kind, digest, job_id, outcome, _json(response)))

    @staticmethod
    def _view(db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["attempts"] = [dict(item) for item in db.execute(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY ordinal", (job_id,))]
        return result

    def submit(self, spec: JobSpec, *, principal: str, key: str):
        body = spec.model_dump(mode="json")
        with self._tx() as db:
            old, digest = self._operation(db, principal, key, "submit", body)
            if old:
                return self._view(db, old["job_id"])
            self.policy.validate_spec(spec)
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE phase='queued' AND intent='run'").fetchone()[0]
            if pending >= self.policy.limits.max_pending:
                raise Conflict("pending capacity reached")
            job_id = uuid.uuid4().hex
            now = _stamp()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, _json(body), 1, "run", "queued", None, "unknown", "awaiting_capacity",
                0, None, now, now))
            self._event(db, job_id, "submitted", {"phase": "queued"})
            self._record_op(db, principal, key, "submit", digest, job_id, "confirmed", {"job_id": job_id})
            return self._view(db, job_id)

    def get(self, job_id):
        try:
            with self._connection() as db:
                db.execute("BEGIN")
                result = self._view(db, job_id)
                db.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    def list(self, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("invalid page size")
        try:
            with self._connection() as db:
                db.execute("BEGIN")
                ids = db.execute("SELECT id FROM jobs ORDER BY created_at,id LIMIT ?", (limit,)).fetchall()
                result = [self._view(db, item[0]) for item in ids]
                db.execute("COMMIT")
                return result
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    def cancel(self, job_id, *, expected_version: int, principal: str, key: str):
        body = {"job_id": job_id, "expected_version": expected_version}
        with self._tx() as db:
            old, digest = self._operation(db, principal, key, "cancel", body)
            if old:
                return self._view(db, old["job_id"])
            job = self._view(db, job_id)
            if job["version"] != expected_version:
                raise Conflict("job version changed")
            if job["intent"] == "cancel":
                original = db.execute("SELECT id FROM operations WHERE job_id=? AND kind='cancel' ORDER BY rowid LIMIT 1", (
                    job_id,)).fetchone()
                if not original:
                    raise Unavailable("cancellation intent has no durable command")
                db.execute("INSERT INTO operation_aliases VALUES(?,?,?,?,?)", (
                    principal, key, "cancel", digest, original["id"]))
                return job
            if job["phase"] == "terminal":
                raise Conflict("job already terminal")
            phase = "terminal" if job["phase"] == "queued" else job["phase"]
            outcome = "cancelled" if phase == "terminal" else None
            db.execute("UPDATE jobs SET intent='cancel',phase=?,outcome=?,reason=? WHERE id=?", (
                phase, outcome, "cancelled_before_launch" if outcome else "stop_requested", job_id))
            self._event(db, job_id, "cancel_requested", {"phase": phase, "outcome": outcome})
            self._record_op(db, principal, key, "cancel", digest, job_id,
                            "confirmed" if outcome else "pending", {"job_id": job_id})
            return self._view(db, job_id)

    def retry(self, job_id, *, expected_version: int, principal: str, key: str):
        """Explicit new attempt only after positive predecessor stop evidence."""
        body = {"job_id": job_id, "expected_version": expected_version}
        with self._tx() as db:
            old, digest = self._operation(db, principal, key, "retry", body)
            if old:
                return self._view(db, old["job_id"])
            job = self._view(db, job_id)
            if job["version"] != expected_version:
                raise Conflict("job version changed")
            previous = job["attempts"][-1] if job["attempts"] else None
            if job["phase"] != "terminal" or not previous or not previous["stopped"]:
                raise Conflict("retry requires a positively stopped terminal attempt")
            active = db.execute("SELECT spec FROM jobs WHERE phase IN ('active','finalizing')").fetchall()
            used_cpu = sum(json.loads(item[0])["resources"]["cpu_millis"] for item in active)
            used_mem = sum(json.loads(item[0])["resources"]["memory_mb"] for item in active)
            request = job["spec"]["resources"]
            if (len(active) >= self.policy.limits.max_active or
                    used_cpu + request["cpu_millis"] > self.policy.limits.cpu_millis or
                    used_mem + request["memory_mb"] > self.policy.limits.memory_mb):
                raise Conflict("active capacity reached")
            attempt_id, incarnation = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute("INSERT INTO attempts(id,job_id,ordinal,incarnation,phase) VALUES(?,?,?,?,?)", (
                attempt_id, job_id, previous["ordinal"] + 1, incarnation, "launch_pending"))
            db.execute("UPDATE jobs SET intent='run',phase='active',outcome=NULL,visibility='unknown',reason='launch_pending',attempt_id=? WHERE id=?", (
                attempt_id, job_id))
            self._event(db, job_id, "retry_launch_intent", {"attempt_id": attempt_id,
                                                            "incarnation": incarnation})
            self._record_op(db, principal, key, "retry", digest, job_id, "pending", {
                "job_id": job_id, "attempt_id": attempt_id})
            return self._view(db, job_id)

    def admit_next(self, *, principal: str, key: str):
        """Durably allocate one attempt and launch intent; caller sends it to supervisor once.

        An uncertain supervisor response is reconciled by the same operation ID.
        """
        with self._tx() as db:
            old, digest = self._operation(db, principal, key, "admit", {})
            if old:
                return self._view(db, old["job_id"])
            active = db.execute("SELECT spec FROM jobs WHERE phase IN ('active','finalizing')").fetchall()
            used_cpu = used_mem = 0
            for item in active:
                request = json.loads(item[0])["resources"]
                used_cpu += request["cpu_millis"]
                used_mem += request["memory_mb"]
            if len(active) >= self.policy.limits.max_active:
                return None
            queued = db.execute("SELECT id,spec FROM jobs WHERE phase='queued' AND intent='run' ORDER BY created_at,id").fetchall()
            for item in queued:
                request = json.loads(item["spec"])["resources"]
                if (used_cpu + request["cpu_millis"] <= self.policy.limits.cpu_millis and
                        used_mem + request["memory_mb"] <= self.policy.limits.memory_mb):
                    job_id = item["id"]
                    break
            else:
                return None
            attempt_id, incarnation = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute("INSERT INTO attempts(id,job_id,ordinal,incarnation,phase) VALUES(?,?,?,?,?)", (
                attempt_id, job_id, 1, incarnation, "launch_pending"))
            db.execute("UPDATE jobs SET phase='active',attempt_id=?,reason='launch_pending' WHERE id=?", (
                attempt_id, job_id))
            self._event(db, job_id, "launch_intent", {"attempt_id": attempt_id, "incarnation": incarnation})
            self._record_op(db, principal, key, "admit", digest, job_id, "pending", {
                "job_id": job_id, "attempt_id": attempt_id})
            return self._view(db, job_id)

    def observe(self, job_id, *, attempt_id: str, incarnation: str, observation_seq: int,
                phase: str, supervisor_id: str, runtime_id: str | None = None,
                stopped: bool = False, exit_code: int | None = None, result_ok: bool | None = None):
        """Apply a supervisor observation. Missing observations never imply an outcome."""
        if phase not in {"starting", "running", "exited", "stopping", "stopped", "unknown"}:
            raise ValueError("invalid observation phase")
        if stopped != (phase in {"exited", "stopped"}):
            raise ValueError("phase and positive stop evidence disagree")
        if observation_seq < 1 or result_ok is not None and (not stopped or exit_code is None):
            raise ValueError("result needs confirmed stop and exit evidence")
        if result_ok is True and exit_code != 0:
            raise ValueError("successful execution requires zero exit")
        if not supervisor_id or len(supervisor_id) > 128:
            raise ValueError("invalid supervisor identity")
        with self._tx() as db:
            job = self._view(db, job_id)
            attempt = db.execute("SELECT * FROM attempts WHERE id=? AND job_id=?", (attempt_id, job_id)).fetchone()
            if not attempt or attempt["incarnation"] != incarnation or job["attempt_id"] != attempt_id:
                raise Conflict("stale or mismatched attempt")
            if observation_seq <= attempt["observation_seq"]:
                return job
            if attempt["stopped"] and not stopped:
                raise Conflict("positive stop evidence cannot be reversed")
            if attempt["supervisor_id"] and attempt["supervisor_id"] != supervisor_id:
                raise Conflict("supervisor identity changed")
            if attempt["runtime_id"] and runtime_id and attempt["runtime_id"] != runtime_id:
                raise Conflict("runtime identity changed")
            if job["phase"] == "terminal":
                raise Conflict("terminal job cannot change")
            outcome = None
            job_phase = "active"
            if stopped:
                job_phase = "terminal" if result_ok is not None or job["intent"] == "cancel" else "finalizing"
                if job_phase == "terminal":
                    outcome = "cancelled" if job["intent"] == "cancel" else "succeeded" if result_ok else "failed"
            db.execute("UPDATE attempts SET phase=?,exit_code=?,supervisor_id=?,runtime_id=COALESCE(runtime_id,?),observation_seq=?,last_observed_at=?,stopped=?,outcome=? WHERE id=?", (
                phase, exit_code, supervisor_id, runtime_id, observation_seq, _stamp(), int(stopped), outcome, attempt_id))
            if phase in {"running", "exited", "stopping", "stopped"}:
                db.execute("UPDATE operations SET outcome='confirmed' WHERE job_id=? AND kind IN ('admit','retry') AND outcome='pending'", (job_id,))
            if stopped and job["intent"] == "cancel":
                db.execute("UPDATE operations SET outcome='confirmed' WHERE job_id=? AND kind='cancel' AND outcome='pending'", (job_id,))
            db.execute("UPDATE jobs SET phase=?,outcome=?,visibility=?,reason=? WHERE id=?", (
                job_phase, outcome, "unknown" if phase == "unknown" else "fresh", phase, job_id))
            self._event(db, job_id, "observed", {"attempt_id": attempt_id, "phase": phase,
                                                  "stopped": stopped, "outcome": outcome,
                                                  "observation_gap": observation_seq > attempt["observation_seq"] + 1})
            return self._view(db, job_id)

    def mark_visibility_unknown(self, job_id, *, expected_version: int):
        with self._tx() as db:
            job = self._view(db, job_id)
            if job["version"] != expected_version:
                raise Conflict("job version changed")
            if job["phase"] != "terminal" and job["visibility"] != "unknown":
                db.execute("UPDATE jobs SET visibility='unknown',reason='observation_unavailable' WHERE id=?", (job_id,))
                self._event(db, job_id, "visibility_unknown", {})
            return self._view(db, job_id)

    def replay(self, job_id, after_seq=0, limit=100):
        if after_seq < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid replay cursor or limit")
        try:
            with self._connection() as db:
                db.execute("BEGIN")
                job = self._view(db, job_id)
                rows = db.execute("SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (
                    job_id, after_seq, limit)).fetchall()
                events = [dict(row) for row in rows]
                for event in events:
                    event["data"] = json.loads(event["data"])
                db.execute("COMMIT")
                return {"snapshot": job, "events": events, "floor": 1,
                        "gap": after_seq < 0, "next_cursor": events[-1]["seq"] if events else after_seq}
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    def pending_outbox(self, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("invalid page size")
        try:
            with self._connection() as db:
                return [dict(row) for row in db.execute(
                    "SELECT * FROM outbox WHERE delivered=0 ORDER BY id LIMIT ?", (limit,))]
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    def ack_outbox(self, outbox_id):
        with self._tx() as db:
            db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (outbox_id,))

    def pending_commands(self, limit=100):
        """Durable dispatch/reconciliation seam for #175's service loop."""
        if not 1 <= limit <= 1000:
            raise ValueError("invalid page size")
        try:
            with self._connection() as db:
                return [dict(row) for row in db.execute(
                    "SELECT * FROM operations WHERE outcome IN ('pending','running','unknown') ORDER BY rowid LIMIT ?", (limit,))]
        except sqlite3.Error as exc:
            raise Unavailable("coordinator store unavailable") from exc

    def set_command_status(self, operation_id: str, status: str):
        """Record ambiguous or in-flight delivery; never infer job success."""
        if status not in {"running", "unknown", "confirmed"}:
            raise ValueError("invalid command status")
        with self._tx() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise KeyError(operation_id)
            if row["outcome"] == "confirmed" and status != "confirmed":
                raise Conflict("confirmed operation cannot regress")
            db.execute("UPDATE operations SET outcome=? WHERE id=?", (status, operation_id))
            return {**dict(row), "outcome": status}
