"""Durable relay tests with a fake independent supervisor control boundary."""
from copy import deepcopy

from coordinator import CoordinatorStore, JobSpec, Limits, Policy
from coordinator.dispatch import ControlError, Dispatcher


class FakeControl:
    def __init__(self):
        self.items = {}
        self.starts = 0
        self.lose_launch_response = False

    def call(self, method, **args):
        if method == "acquire":
            return {"supervisor_id": "supervisor-one", "generation": 1}
        if method == "renew":
            return 123.0
        attempt_id = args.get("attempt_id")
        if method == "inspect":
            if attempt_id not in self.items:
                raise ControlError("KeyError")
            return deepcopy(self.items[attempt_id])
        if method == "launch":
            plan = args["plan"]
            attempt_id = plan["attempt_id"]
            assert attempt_id not in self.items
            self.starts += 1
            self.items[attempt_id] = {"id": attempt_id, "job_id": plan["job_id"],
                "incarnation": plan["incarnation"], "plan": deepcopy(plan),
                "state": "running", "runtime_id": "runtime-" + attempt_id,
                "observation_seq": 0}
            if self.lose_launch_response:
                self.lose_launch_response = False
                raise ControlError("OwnershipUnknown")
            return deepcopy(self.items[attempt_id])
        if method == "reconcile":
            if attempt_id not in self.items:
                raise ControlError("KeyError")
            item = self.items[attempt_id]
            item["observation_seq"] += 1
            return deepcopy(item)
        if method == "cancel":
            item = self.items[attempt_id]
            item["state"] = "stopped"
            return deepcopy(item)
        raise AssertionError(method)


def setup(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    policy = Policy(profiles=frozenset({"offline"}), workspaces=frozenset({"scratch"}),
                    worker_types=frozenset({"command"}),
                    limits=Limits(max_pending=4, max_active=2, cpu_millis=2000, memory_mb=512))
    store = CoordinatorStore(root, policy)
    spec = JobSpec(worker_type="command", profile_ref="offline", workspace_ref="scratch",
                   payload={"argv": ["true"]}, deadline_seconds=30,
                   resources={"cpu_millis": 1000, "memory_mb": 128})
    return store, spec


def test_relay_same_attempt_and_confirm_cancel(tmp_path):
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    assert dispatcher.tick()["admitted"] == job["id"]
    dispatcher.tick()
    running = store.get(job["id"])
    assert running["phase"] == "active" and running["visibility"] == "fresh"
    assert control.starts == 1
    seq = running["seq"]
    dispatcher.tick()
    assert control.starts == 1 and store.get(job["id"])["seq"] == seq
    store.cancel(job["id"], expected_version=running["version"], principal="owner", key="cancel")
    dispatcher.tick()
    cancelled = store.get(job["id"])
    assert cancelled["phase"] == "terminal" and cancelled["outcome"] == "cancelled"
    assert control.starts == 1


def test_prelaunch_cancel_never_starts_worker(tmp_path):
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()  # durable admission, no runtime call yet
    active = store.get(job["id"])
    store.cancel(job["id"], expected_version=active["version"], principal="owner", key="cancel")
    dispatcher.tick()
    assert control.starts == 0
    assert store.get(job["id"])["phase"] == "active"  # no invented stop evidence


def test_stopped_without_result_is_finalizing(tmp_path):
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    attempt_id = store.get(job["id"])["attempt_id"]
    control.items[attempt_id]["state"] = "stopped"
    dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "finalizing" and result["outcome"] is None


def test_bounded_supervisor_wire_and_error(monkeypatch):
    import io
    from coordinator.dispatch import SupervisorControl
    from coordinator import dispatch

    class Socket:
        def __init__(self):
            self.wire = b""
            self.answer = b'{"ok":true,"result":{"generation":3}}\n'
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def settimeout(self, timeout):
            assert timeout == 2
        def connect(self, path):
            assert path == "/private/control.sock"
        def sendall(self, data):
            self.wire = data
        def makefile(self, mode):
            return io.BytesIO(self.answer)

    fake = Socket()
    monkeypatch.setattr(dispatch.socket, "socket", lambda *_: fake)
    control = SupervisorControl("/private/control.sock", timeout=2)
    monkeypatch.setattr(control, "_check_socket", lambda: None)
    assert control.call("acquire", controller="one", lease_seconds=15)["generation"] == 3
    assert b'"method":"acquire"' in fake.wire
    fake.answer = b'{"ok":false,"error":"Fenced"}\n'
    try:
        control.call("renew", controller="one", generation=2)
    except ControlError as exc:
        assert exc.code == "Fenced"
    else:
        raise AssertionError("fenced operation accepted")
    try:
        control.call("owner_stop", attempt_id="x")
    except ValueError:
        pass
    else:
        raise AssertionError("owner operation crossed control boundary")


def test_lost_launch_response_reconciles_same_attempt(tmp_path):
    import pytest
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    control.lose_launch_response = True
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    assert control.starts == 1
    assert store.get(job["id"])["visibility"] == "unknown"
    dispatcher.tick()
    assert control.starts == 1
    assert store.get(job["id"])["visibility"] == "fresh"
