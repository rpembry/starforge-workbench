"""Deterministic supervisor failures without a Docker socket."""

import pytest

from coordinator.supervisor import Fenced, OwnershipUnknown, Supervisor


class Runtime:
    def __init__(self):
        self.items = {}
        self.starts = 0
        self.stops = 0
        self.fail_after_create = False

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


def test_lost_launch_response_does_not_duplicate(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    runtime.fail_after_create = True
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")
    assert supervisor.inspect("attempt-1")["state"] == "unknown"
    assert supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")["state"] == "unknown"
    assert runtime.starts == 1
    same = supervisor.reconcile("attempt-1")
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


def test_clock_rewind_blocks_new_start_but_stops_existing(setup):
    supervisor, runtime, clock = setup
    lease = supervisor.acquire("a")
    supervisor.launch(plan(), controller="a", generation=lease["generation"], operation_id="launch-1")
    clock[0] -= 1
    assert supervisor.tick() == ["attempt-1"]
    with pytest.raises(OwnershipUnknown):
        supervisor.acquire("b", owner_takeover=True)
    assert runtime.stops == 1
