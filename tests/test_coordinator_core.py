"""Source-only state tests; no Docker, Workbench, provider, or live services."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from coordinator import Conflict, CoordinatorStore, JobSpec, Limits, Policy, Unavailable


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    policy = Policy(profiles=frozenset({"offline"}), workspaces=frozenset({"scratch"}),
                    worker_types=frozenset({"command"}),
                    limits=Limits(max_pending=2, max_active=1, cpu_millis=1000, memory_mb=256))
    return CoordinatorStore(root, policy)


@pytest.fixture
def spec():
    return JobSpec(worker_type="command", profile_ref="offline", workspace_ref="scratch",
                   payload={"argv": ["true"]}, deadline_seconds=30,
                   resources={"cpu_millis": 500, "memory_mb": 128},
                   parent_ref="opaque-upstream-id")


def test_schema_and_approval(store, spec):
    assert JobSpec.model_validate_json(spec.model_dump_json()) == spec
    assert "properties" in JobSpec.model_json_schema()
    with pytest.raises(ValidationError):
        JobSpec.model_validate({**spec.model_dump(), "gpu": 1})
    with pytest.raises(ValidationError):
        JobSpec.model_validate({**spec.model_dump(), "payload": {"blob": "x" * 65536}})
    with pytest.raises(ValueError, match="unapproved"):
        store.submit(spec.model_copy(update={"workspace_ref": "other"}), principal="a", key="b")
    assert store.submit(spec, principal="a", key="b")["phase"] == "queued"
    assert store.get(store.list()[0]["id"])["attempts"] == []  # parent ref grants no launch authority


def test_duplicate_submit_and_outbox_are_durable(store, spec):
    first = store.submit(spec, principal="a", key="one")
    assert store.submit(spec, principal="a", key="one")["id"] == first["id"]
    assert len(store.list()) == 1
    with pytest.raises(Conflict):
        store.submit(spec.model_copy(update={"deadline_seconds": 31}), principal="a", key="one")
    assert len(store.pending_outbox()) == 1
    event = store.pending_outbox()[0]
    store.ack_outbox(event["id"])
    store.ack_outbox(event["id"])
    assert store.pending_outbox() == []
    again = CoordinatorStore(store.root, store.policy)
    assert again.get(first["id"])["id"] == first["id"]
    assert again.replay(first["id"], 0)["events"][0]["kind"] == "submitted"


def test_capacity_cancel_and_stale_version(store, spec):
    a = store.submit(spec, principal="client", key="a")
    b = store.submit(spec, principal="client", key="b")
    with pytest.raises(Conflict, match="pending capacity"):
        store.submit(spec, principal="client", key="c")
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(lambda k: store.admit_next(principal="scheduler", key=k), ("1", "2")))
    assert sum(item is not None for item in admitted) == 1
    running = next(item for item in admitted if item)
    waiting = b if running["id"] == a["id"] else a
    assert store.admit_next(principal="scheduler", key="3") is None
    cancelled = store.cancel(waiting["id"], expected_version=waiting["version"], principal="client", key="stop")
    assert cancelled["phase"] == "terminal" and cancelled["outcome"] == "cancelled"
    with pytest.raises(Conflict, match="version"):
        store.cancel(running["id"], expected_version=1, principal="client", key="stale")
    assert store.admit_next(principal="scheduler", key="4") is None


def test_unknown_evidence_retry_and_stale_observation(store, spec):
    job = store.submit(spec, principal="client", key="a")
    job = store.admit_next(principal="scheduler", key="a")
    attempt = job["attempts"][0]
    args = dict(job_id=job["id"], attempt_id=attempt["id"], incarnation=attempt["incarnation"],
                supervisor_id="host-1")
    with pytest.raises(Conflict):
        store.retry(job["id"], expected_version=job["version"], principal="client", key="early")
    job = store.observe(**args, observation_seq=1, phase="running", runtime_id="container-1")
    job = store.mark_visibility_unknown(job["id"], expected_version=job["version"])
    assert job["visibility"] == "unknown" and job["outcome"] is None
    assert store.observe(**args, observation_seq=1, phase="running") == job
    with pytest.raises(ValueError, match="result needs"):
        store.observe(**args, observation_seq=2, phase="running", result_ok=True)
    with pytest.raises(Conflict, match="runtime identity"):
        store.observe(**args, observation_seq=2, phase="running", runtime_id="other")
    job = store.observe(**args, observation_seq=2, phase="stopped", runtime_id="container-1",
                        stopped=True, exit_code=1, result_ok=False)
    assert job["phase"] == "terminal" and job["outcome"] == "failed"
    retried = store.retry(job["id"], expected_version=job["version"], principal="client", key="retry")
    assert retried["attempt_id"] != attempt["id"]
    assert retried["attempts"][0]["outcome"] == "failed"
    with pytest.raises(Conflict, match="stale"):
        store.observe(**args, observation_seq=3, phase="running")
    assert [e["seq"] for e in store.replay(job["id"], 0)["events"]] == list(range(1, retried["seq"] + 1))


def test_storage_failure_never_acknowledges_submit(store, spec, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(sqlite3, "connect", broken)
    with pytest.raises(Unavailable):
        store.submit(spec, principal="client", key="a")


def test_dispatch_command_identity_survives_restart(store, spec):
    store.submit(spec, principal="client", key="a")
    job = store.admit_next(principal="scheduler", key="admit-a")
    pending = store.pending_commands()
    assert len(pending) == 1 and pending[0]["kind"] == "admit"
    assert pending[0]["job_id"] == job["id"]
    restarted = CoordinatorStore(store.root, store.policy)
    assert restarted.pending_commands()[0]["id"] == pending[0]["id"]
    restarted.set_command_status(pending[0]["id"], "unknown")
    assert restarted.pending_commands()[0]["outcome"] == "unknown"
    restarted.set_command_status(pending[0]["id"], "confirmed")
    assert restarted.pending_commands() == []
    with pytest.raises(Conflict):
        restarted.set_command_status(pending[0]["id"], "running")


def test_stopped_evidence_cannot_be_reversed_or_contradicted(store, spec):
    store.submit(spec, principal="client", key="a")
    job = store.admit_next(principal="scheduler", key="a")
    attempt = job["attempts"][0]
    args = dict(job_id=job["id"], attempt_id=attempt["id"],
                incarnation=attempt["incarnation"], supervisor_id="host")
    with pytest.raises(ValueError, match="disagree"):
        store.observe(**args, observation_seq=1, phase="running", stopped=True,
                      exit_code=0, result_ok=True)
    stopped = store.observe(**args, observation_seq=1, phase="exited", stopped=True,
                            exit_code=0)
    assert stopped["phase"] == "finalizing"
    with pytest.raises(Conflict, match="cannot be reversed"):
        store.observe(**args, observation_seq=2, phase="unknown", stopped=False)
    assert store.get(job["id"])["attempts"][0]["stopped"] == 1
    final = store.observe(**args, observation_seq=2, phase="stopped", stopped=True,
                          exit_code=0, result_ok=True)
    assert final["outcome"] == "succeeded"


def test_same_submit_key_replays_after_policy_revocation(store, spec):
    original = store.submit(spec, principal="client", key="first")
    denied = Policy(profiles=frozenset(), workspaces=store.policy.workspaces,
                    worker_types=store.policy.worker_types, limits=store.policy.limits)
    reopened = CoordinatorStore(store.root, denied)
    assert reopened.submit(spec, principal="client", key="first")["id"] == original["id"]
    with pytest.raises(ValueError, match="unapproved"):
        reopened.submit(spec, principal="client", key="new")


def test_repeated_cancel_key_aliases_original_operation(store, spec):
    submitted = store.submit(spec, principal="client", key="submit")
    job = store.admit_next(principal="scheduler", key="admit")
    cancelled = store.cancel(job["id"], expected_version=job["version"],
                             principal="client", key="cancel-one")
    repeated = store.cancel(job["id"], expected_version=cancelled["version"],
                            principal="client", key="cancel-two")
    assert repeated["version"] == cancelled["version"]
    commands = [op for op in store.pending_commands() if op["kind"] == "cancel"]
    assert len(commands) == 1
    assert store.cancel(job["id"], expected_version=cancelled["version"],
                        principal="client", key="cancel-two")["id"] == submitted["id"]
    with pytest.raises(Conflict, match="different command"):
        store.cancel(job["id"], expected_version=job["version"],
                     principal="client", key="cancel-two")


def test_schema_one_migration_preserves_jobs(store, spec):
    original = store.submit(spec, principal="client", key="first")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE operation_aliases")
        db.execute("ALTER TABLE attempts DROP COLUMN no_start_reason")
        db.execute("PRAGMA user_version=1")
    reopened = CoordinatorStore(store.root, store.policy)
    assert reopened.get(original["id"])["id"] == original["id"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_positive_no_start_terminal_replay_and_retry(store, spec):
    job = store.submit(spec, principal="client", key="no-start")
    job = store.admit_next(principal="scheduler", key="no-start")
    attempt = job["attempts"][0]
    evidence = dict(job_id=job["id"], attempt_id=attempt["id"],
                    incarnation=attempt["incarnation"], supervisor_id="host-1",
                    observation_seq=2, reason="workspace_setup_failed")
    with pytest.raises(Conflict):
        store.retry(job["id"], expected_version=job["version"], principal="client", key="early-no-start")
    observed = store.record_no_start(**evidence)
    assert observed["phase"] == "terminal" and observed["outcome"] == "failed"
    assert observed["reason"] == "workspace_setup_failed"
    assert observed["attempts"][0]["exit_code"] is None
    assert observed["attempts"][0]["runtime_id"] is None
    assert observed["attempts"][0]["stopped"] == 1
    assert store.record_no_start(**evidence) == observed
    with pytest.raises(Conflict):
        store.record_no_start(**{**evidence, "observation_seq": 1})
    with pytest.raises(Conflict):
        store.record_no_start(**{**evidence, "reason": "prelaunch_abandon"})
    assert store.replay(job["id"], 0)["events"][-1]["kind"] == "no_start_observed"
    retried = store.retry(job["id"], expected_version=observed["version"],
                          principal="client", key="after-no-start")
    assert retried["attempt_id"] != attempt["id"]
    with pytest.raises(Conflict):
        store.record_no_start(**{**evidence, "observation_seq": 3})


def test_no_start_respects_sticky_cancel_and_rejects_runtime_evidence(store, spec):
    job = store.submit(spec, principal="client", key="cancel-no-start")
    job = store.admit_next(principal="scheduler", key="cancel-no-start")
    attempt = job["attempts"][0]
    args = dict(job_id=job["id"], attempt_id=attempt["id"],
                incarnation=attempt["incarnation"], supervisor_id="host-1")
    job = store.cancel(job["id"], expected_version=job["version"],
                       principal="client", key="cancel-no-start-request")
    observed = store.record_no_start(**args, observation_seq=1, reason="prelaunch_abandon")
    assert observed["outcome"] == "cancelled"
    assert observed["attempts"][0]["outcome"] == "cancelled"
    other = store.submit(spec, principal="client", key="runtime-conflict")
    other = store.admit_next(principal="scheduler", key="runtime-conflict")
    assert other is not None
    current = other["attempts"][0]
    store.observe(other["id"], attempt_id=current["id"], incarnation=current["incarnation"],
                  observation_seq=1, supervisor_id="host-1", phase="running", runtime_id="container")
    with pytest.raises(Conflict):
        store.record_no_start(other["id"], attempt_id=current["id"],
                              incarnation=current["incarnation"], observation_seq=2,
                              supervisor_id="host-1", reason="workspace_setup_failed")


def test_no_start_store_failure_cannot_confirm_terminal(store, spec, monkeypatch):
    store.submit(spec, principal="client", key="failed-write")
    job = store.admit_next(principal="scheduler", key="failed-write")
    attempt = job["attempts"][0]
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(sqlite3, "connect", broken)
    with pytest.raises(Unavailable):
        store.record_no_start(job["id"], attempt_id=attempt["id"],
                              incarnation=attempt["incarnation"], observation_seq=1,
                              supervisor_id="host-1", reason="workspace_setup_failed")
    monkeypatch.undo()
    assert store.get(job["id"])["phase"] == "active"


def test_reattach_replays_same_attempt_and_command(store, spec):
    submitted = store.submit(spec, principal="client", key="submit")
    active = store.admit_next(principal="scheduler", key="admit")
    attached = store.reattach(submitted["id"], expected_version=active["version"],
                              principal="client", key="reattach")
    assert attached["attempt_id"] == active["attempt_id"]
    commands = [item for item in store.pending_commands() if item["kind"] == "reattach"]
    assert len(commands) == 1
    with pytest.raises(Conflict, match="version"):
        store.reattach(submitted["id"], expected_version=active["version"],
                       principal="client", key="new-key")
    replay = CoordinatorStore(store.root, store.policy).reattach(
        submitted["id"], expected_version=active["version"], principal="client", key="reattach")
    assert replay["attempt_id"] == active["attempt_id"]
    assert [item["id"] for item in store.pending_commands() if item["kind"] == "reattach"] == [commands[0]["id"]]


def test_old_reattach_key_never_targets_explicit_retry(store, spec):
    job = store.submit(spec, principal="client", key="submit")
    active = store.admit_next(principal="scheduler", key="admit")
    original = store.reattach(job["id"], expected_version=active["version"],
                              principal="client", key="reattach-old")
    attempt = active["attempts"][0]
    ended = store.observe(job["id"], attempt_id=attempt["id"], incarnation=attempt["incarnation"],
                          supervisor_id="host", observation_seq=1, phase="stopped", stopped=True,
                          exit_code=1, result_ok=False)
    retried = store.retry(job["id"], expected_version=ended["version"],
                          principal="client", key="retry")
    assert retried["attempt_id"] != original["attempt_id"]
    replay = store.reattach(job["id"], expected_version=active["version"],
                            principal="client", key="reattach-old")
    assert replay == original
    assert replay["attempt_id"] != store.get(job["id"])["attempt_id"]
