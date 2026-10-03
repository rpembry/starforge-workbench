"""Deterministic supervisor failures without a Docker socket."""

import pytest
import sqlite3
import hashlib
import json

from coordinator.supervisor import Conflict, Fenced, OwnershipUnknown, RecoveryUncertain, Supervisor, WatchdogUncertain
from coordinator.store import Unavailable


class Runtime:
    def __init__(self):
        self.items = {}
        self.starts = 0
        self.stops = 0
        self.fail_after_create = False
        self.ready = False
        self.fail_ready = False

    def validate(self, plan):
        if plan["profile_ref"] != "offline" or plan["workspace_ref"] != "scratch":
            raise ValueError("unapproved host policy")

    def launch(self, plan):
        key = plan["attempt_id"]
        assert key not in self.items
        self.starts += 1
        self.items[key] = {"identity_ok": True, "running": True, "stopped": False,
                           "exit_code": None, "runtime_id": "runtime-" + key}
        if self.fail_after_create:
            raise TimeoutError("response lost")
        return self.items[key]["runtime_id"]

    def inspect(self, plan, runtime_id):
        return dict(self.items[plan["attempt_id"]])

    def stop(self, plan, runtime_id):
        item = self.items[plan["attempt_id"]]
        assert runtime_id == item["runtime_id"] and item["identity_ok"]
        self.stops += 1
        item.update(running=False, stopped=True, exit_code=143)

    def protocol_ready(self, plan, runtime_id):
        assert runtime_id == self.items[plan["attempt_id"]]["runtime_id"]
        if self.fail_ready:
            raise OSError("inbox unavailable")
        return self.ready


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    runtime = Runtime()
    clock = [1000.0]
    supervisor = Supervisor(root, runtime, clock=lambda: clock[0])
    return supervisor, runtime, clock


def plan(attempt="attempt-1", mode="strict"):
    return {"job_id": "job-1", "attempt_id": attempt, "incarnation": "inc-1",
            "worker_type": "command", "profile_ref": "offline", "workspace_ref": "scratch",
            "payload": {"argv": ["true"]},
            "deadline_seconds": 30, "orphan_policy": {
                "mode": mode, "grace_seconds": 3,
                "max_orphan_seconds": 12 if mode == "trusted_local" else 0}}


def test_fenced_launch_and_owner_stop(setup):
    supervisor, runtime, clock = setup
    a = supervisor.acquire("a", lease_seconds=5)
    with pytest.raises(Fenced):
        supervisor.acquire("b")
    item = supervisor.launch(plan(), controller="a", generation=a["generation"], operation_id="launch-1")
    assert item["state"] == "running"
    assert supervisor.launch(plan(), controller="a", generation=a["generation"], operation_id="launch-1")["runtime_id"] == item["runtime_id"]
    assert runtime.starts == 1
    stopped = supervisor.owner_stop("attempt-1", operation_id="owner-1")
    assert stopped["state"] == "stopped" and stopped["cancel"]
    assert supervisor.owner_stop("attempt-1", operation_id="owner-1")["state"] == "stopped"
    assert runtime.stops == 1
    with pytest.raises(Fenced):
        supervisor.cancel("attempt-1", controller="a", generation=a["generation"], operation_id="late")
    b = supervisor.acquire("b")
    assert b["generation"] > a["generation"]
    with pytest.raises(Fenced):
        supervisor.renew("a", a["generation"])


@pytest.mark.parametrize("mode,stop_at", [("strict", 1008), ("trusted_local", 1012)])
def test_orphan_budget_survives_restart(setup, mode, stop_at):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a", lease_seconds=5)
    supervisor.launch(plan(mode=mode), controller="a", generation=lease["generation"], operation_id="launch-1")
    restarted = Supervisor(supervisor.root, runtime, clock=lambda: clock[0])
    clock[0] = stop_at - 0.1
    assert restarted.tick() == []
    clock[0] = stop_at
    assert restarted.tick() == ["attempt-1"]
    assert runtime.stops == 1
    assert restarted.inspect("attempt-1")["state"] == "stopped"


def test_watchdog_observes_normal_exit_without_controller(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch")
    runtime.items["attempt-1"].update(running=False, stopped=True, exit_code=0)
    clock[0] = 1016  # coordinator lease expired; strict orphan grace not yet due
    assert supervisor.tick() == ["attempt-1"]
    assert supervisor.inspect("attempt-1")["state"] == "stopped"
    assert runtime.stops == 0


def test_protocol_readiness_timeout_is_bounded_and_owner_enforced(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    worker = plan(mode="trusted_local")
    worker["worker_type"] = "protocol_example"
    worker["deadline_seconds"] = 60
    worker["orphan_policy"]["max_orphan_seconds"] = 60
    supervisor.launch(worker, controller="a", generation=lease["generation"], operation_id="launch")
    clock[0] = 1029.9
    assert supervisor.tick() == []
    clock[0] = 1030
    assert supervisor.tick() == ["attempt-1"]
    assert runtime.stops == 1 and supervisor.inspect("attempt-1")["cancel"] == 1


def test_protocol_ready_survives_early_window(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    worker = plan(mode="trusted_local")
    worker["worker_type"] = "protocol_example"
    worker["deadline_seconds"] = 60
    worker["orphan_policy"]["max_orphan_seconds"] = 60
    supervisor.launch(worker, controller="a", generation=lease["generation"], operation_id="launch")
    runtime.ready = True
    clock[0] = 1030
    assert supervisor.tick() == []
    assert supervisor.inspect("attempt-1")["state"] == "running"


def test_readiness_observation_failure_stops_at_cap_not_job_deadline(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    worker = plan(mode="trusted_local")
    worker["worker_type"] = "protocol_example"
    worker["deadline_seconds"] = 60
    worker["orphan_policy"]["max_orphan_seconds"] = 60
    supervisor.launch(worker, controller="a", generation=lease["generation"], operation_id="launch")
    runtime.fail_ready = True
    clock[0] = 1029
    with pytest.raises(WatchdogUncertain) as early:
        supervisor.tick()
    assert early.value.uncertain == ["attempt-1"] and runtime.stops == 0
    clock[0] = 1030
    with pytest.raises(WatchdogUncertain) as capped:
        supervisor.tick()
    assert capped.value.stopped == ["attempt-1"]
    assert capped.value.uncertain == ["attempt-1"]
    assert runtime.stops == 1 and supervisor.inspect("attempt-1")["state"] == "stopped"


def test_lost_launch_response_does_not_duplicate(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    runtime.fail_after_create = True
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")
    assert supervisor.inspect("attempt-1")["state"] == "unknown"
    assert supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")["state"] == "unknown"
    assert runtime.starts == 1
    same = supervisor.reconcile("attempt-1", controller="a", generation=lease["generation"])
    assert same["state"] == "running" and same["runtime_id"] == "runtime-attempt-1"
    assert runtime.starts == 1


def test_identity_mismatch_blocks_stop(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")
    runtime.items["attempt-1"]["identity_ok"] = False
    with pytest.raises(OwnershipUnknown):
        supervisor.owner_stop("attempt-1", operation_id="stop-1")
    assert supervisor.inspect("attempt-1")["state"] == "unknown"
    assert supervisor.inspect("attempt-1")["cancel"] == 1
    assert runtime.stops == 0


def test_owner_stop_storage_failure_only_stops_exact_saved_runtime(setup, monkeypatch):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch")
    def failed_tx():
        raise Unavailable("journal write failed")
    monkeypatch.setattr(supervisor, "_tx", failed_tx)
    with pytest.raises(OwnershipUnknown, match="uncertain"):
        supervisor.owner_stop("attempt-1", operation_id="emergency")
    assert runtime.stops == 1
    assert supervisor.inspect("attempt-1")["state"] == "running"
    assert supervisor.inspect("attempt-1")["cancel"] == 0


def test_owner_stop_storage_failure_never_stops_mismatched_runtime(setup, monkeypatch):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch")
    runtime.items["attempt-1"]["runtime_id"] = "different"
    def failed_tx():
        raise Unavailable("journal write failed")
    monkeypatch.setattr(supervisor, "_tx", failed_tx)
    with pytest.raises(OwnershipUnknown, match="uncertain"):
        supervisor.owner_stop("attempt-1", operation_id="emergency")
    assert runtime.stops == 0


def test_owner_stop_storage_failure_does_not_adopt_unsaved_runtime(setup, monkeypatch):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    runtime.fail_after_create = True
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch")
    assert supervisor.inspect("attempt-1")["runtime_id"] is None
    def failed_tx():
        raise Unavailable("journal write failed")
    monkeypatch.setattr(supervisor, "_tx", failed_tx)
    with pytest.raises(OwnershipUnknown, match="uncertain"):
        supervisor.owner_stop("attempt-1", operation_id="emergency")
    assert runtime.stops == 0 and runtime.items["attempt-1"]["running"]


def test_clock_rewind_blocks_new_start_but_stops_existing(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")
    clock[0] -= 1
    assert supervisor.tick() == ["attempt-1"]
    with pytest.raises(OwnershipUnknown):
        supervisor.acquire("b", owner_takeover=True)
    assert runtime.stops == 1


def test_reacquired_lease_cannot_extend_strict_orphan_stop(setup):
    supervisor, runtime, clock = setup
    original = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan(), controller="old", generation=original["generation"], operation_id="launch")
    clock[0] = 1006
    supervisor.acquire("new", lease_seconds=30)
    clock[0] = 1008
    assert supervisor.tick() == ["attempt-1"]
    assert runtime.stops == 1


def test_timely_renewal_extends_only_same_generation_budget(setup):
    supervisor, runtime, clock = setup
    original = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan(), controller="old", generation=original["generation"], operation_id="launch")
    clock[0] = 1004
    supervisor.renew("old", original["generation"], lease_seconds=5)
    clock[0] = 1008
    assert supervisor.tick() == []
    clock[0] = 1012
    assert supervisor.tick() == ["attempt-1"]


def test_duplicate_owner_stop_does_not_fence_new_controller(setup):
    supervisor, runtime, clock = setup
    original = supervisor.acquire("old")
    supervisor.launch(plan(), controller="old", generation=original["generation"], operation_id="launch")
    supervisor.owner_stop("attempt-1", operation_id="owner-stop")
    new = supervisor.acquire("new")
    supervisor.owner_stop("attempt-1", operation_id="owner-stop")
    assert supervisor.renew("new", new["generation"]) > clock[0]


def test_watchdog_continues_after_one_ownership_mismatch(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan("attempt-1"), controller="old", generation=lease["generation"], operation_id="launch-1")
    supervisor.launch(plan("attempt-2"), controller="old", generation=lease["generation"], operation_id="launch-2")
    runtime.items["attempt-1"]["identity_ok"] = False
    clock[0] = 1008
    with pytest.raises(WatchdogUncertain) as caught:
        supervisor.tick()
    assert caught.value.stopped == ["attempt-2"]
    assert caught.value.uncertain == ["attempt-1"]
    assert runtime.items["attempt-1"]["running"]
    assert runtime.items["attempt-2"]["stopped"]


def test_watchdog_continues_after_transient_journal_read_failure(setup, monkeypatch):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan("attempt-1"), controller="old", generation=lease["generation"], operation_id="launch-1")
    supervisor.launch(plan("attempt-2"), controller="old", generation=lease["generation"], operation_id="launch-2")
    clock[0] = 1008
    original = sqlite3.connect
    calls = [0]
    def flaky(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:  # tick transaction works; first exact inspect fails
            raise sqlite3.OperationalError("transient read failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", flaky)
    with pytest.raises(WatchdogUncertain) as caught:
        supervisor.tick()
    assert caught.value.uncertain == ["attempt-1"]
    assert caught.value.stopped == ["attempt-2"]
    assert runtime.items["attempt-1"]["running"]
    assert runtime.items["attempt-2"]["stopped"]


def test_prelaunch_abandon_fences_delayed_launch_without_start(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("controller")
    abandoned = supervisor.abandon(plan(), controller="controller",
                                   generation=lease["generation"], operation_id="launch-op")
    assert abandoned["state"] == "stopped" and abandoned["cancel"] == 1
    assert abandoned["runtime_id"] is None and runtime.starts == 0
    assert supervisor.launch(plan(), controller="controller",
                             generation=lease["generation"], operation_id="launch-op")["state"] == "stopped"
    assert supervisor.reconcile("attempt-1", controller="controller", generation=lease["generation"])["state"] == "stopped"
    assert runtime.starts == 0
    with pytest.raises(Conflict):
        supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="new-op")


def test_abandon_after_launch_stops_same_runtime(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("controller")
    supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="launch-op")
    stopped = supervisor.abandon(plan(), controller="controller",
                                 generation=lease["generation"], operation_id="launch-op")
    assert stopped["state"] == "stopped" and runtime.starts == 1 and runtime.stops == 1


def test_restart_blocks_new_launch_until_exact_recovery(setup):
    supervisor, runtime, clock = setup
    original = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan("attempt-1"), controller="old", generation=original["generation"], operation_id="launch-1")
    clock[0] = 1006
    restarted = Supervisor(supervisor.root, runtime, clock=lambda: clock[0])
    assert restarted.recovery_required()
    fresh = restarted.acquire("new")
    with pytest.raises(RecoveryUncertain):
        restarted.launch(plan("attempt-2"), controller="new", generation=fresh["generation"], operation_id="launch-2")
    assert runtime.starts == 1
    assert restarted.recover_startup() == ["attempt-1"]
    assert not restarted.recovery_required()
    restarted.launch(plan("attempt-2"), controller="new", generation=fresh["generation"], operation_id="launch-2")
    assert runtime.starts == 2


def test_stale_controller_cannot_mutate_reconciliation(setup):
    supervisor, runtime, clock = setup
    old = supervisor.acquire("old", lease_seconds=5)
    supervisor.launch(plan(), controller="old", generation=old["generation"], operation_id="launch")
    seq = supervisor.inspect("attempt-1")["observation_seq"]
    clock[0] = 1006
    new = supervisor.acquire("new")
    with pytest.raises(Fenced):
        supervisor.reconcile("attempt-1", controller="old", generation=old["generation"])
    assert supervisor.inspect("attempt-1")["observation_seq"] == seq
    assert supervisor.reconcile("attempt-1", controller="new", generation=new["generation"])["observation_seq"] == seq + 1


def test_supervisor_schema_two_migrates_without_losing_identity(setup):
    supervisor, runtime, clock = setup
    identity = supervisor.acquire("controller")["supervisor_id"]
    with sqlite3.connect(supervisor.path) as db:
        db.execute("ALTER TABLE meta DROP COLUMN recovery_required")
        db.execute("PRAGMA user_version=2")
    reopened = Supervisor(supervisor.root, runtime, clock=lambda: clock[0])
    assert reopened.acquire("controller")["supervisor_id"] == identity
    with sqlite3.connect(supervisor.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_owner_stop_confirms_no_start_from_committed_launch_pending(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("controller")
    pending_plan = plan()
    digest = hashlib.sha256(json.dumps(pending_plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with supervisor._tx() as db:
        db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "attempt-1", "job-1", "inc-1", digest,
            json.dumps(pending_plan, sort_keys=True, separators=(",", ":")),
            lease["generation"], "launch-op", None, "launch_pending", 0,
            1030, 1018, "strict", 3, 0))
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (
            "launch-op", "attempt-1", "launch", "pending", None))
    stopped = supervisor.owner_stop("attempt-1", operation_id="owner-op")
    assert stopped["state"] == "stopped" and stopped["runtime_id"] is None
    assert runtime.starts == 0
    with supervisor._db() as db:
        launch = db.execute("SELECT status,response FROM operations WHERE id='launch-op'").fetchone()
    assert launch["status"] == "confirmed"
    assert json.loads(launch["response"]) == {"state": "stopped", "no_start": True}
