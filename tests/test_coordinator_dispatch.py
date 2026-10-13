"""Durable relay tests with a fake independent supervisor control boundary."""
from copy import deepcopy
import base64
import hashlib

from coordinator import CoordinatorStore, JobSpec, Limits, Policy, Unavailable
from coordinator.dispatch import ControlError, Dispatcher, SupervisorEvidence
from coordinator.supervisor import PlanRejected


class FakeControl:
    def __init__(self):
        self.items = {}
        self.starts = 0
        self.lose_launch_response = False
        self.result_ok = None
        self.exit_code = None
        self.output = b"hello\n"

    def call(self, method, **args):
        if method == "acquire":
            return {"supervisor_id": "supervisor-one", "generation": 1}
        if method == "renew":
            return 123.0
        if method == "reconcile":
            assert args["controller"] and args["generation"] == 1
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
        if method == "abandon":
            plan = args["plan"]
            attempt_id = plan["attempt_id"]
            assert args["operation_id"]
            item = self.items.get(attempt_id)
            if item is None:
                item = {"id": attempt_id, "job_id": plan["job_id"],
                    "incarnation": plan["incarnation"], "plan": deepcopy(plan),
                    "state": "stopped", "runtime_id": None, "observation_seq": 1,
                    "no_start_reason": "prelaunch_abandon"}
                self.items[attempt_id] = item
            else:
                item["state"] = "stopped"
                item["observation_seq"] += 1
            return deepcopy(item)
        if method == "reconcile":
            if attempt_id not in self.items:
                raise ControlError("KeyError")
            item = self.items[attempt_id]
            item["observation_seq"] += 1
            return deepcopy(item)
        if method == "collect":
            item = self.items[attempt_id]
            item["observation_seq"] += 1
            if item.get("no_start_reason"):
                return {"supervisor_id": "supervisor-one", "job_id": item["job_id"],
                        "attempt_id": attempt_id, "incarnation": item["incarnation"],
                        "runtime_id": None, "observation_seq": item["observation_seq"],
                        "phase": "stopped", "stopped": True, "exit_code": None,
                        "result_ok": False, "no_start": True,
                        "no_start_reason": item["no_start_reason"]}
            return {"supervisor_id": "supervisor-one", "job_id": item["job_id"],
                    "attempt_id": attempt_id, "incarnation": item["incarnation"],
                    "runtime_id": item["runtime_id"], "observation_seq": item["observation_seq"],
                    "phase": "stopped", "stopped": True, "exit_code": self.exit_code,
                    "result_ok": self.result_ok, "artifact_manifest": {"files": [
                        {"path": "output.txt", "bytes": len(self.output),
                         "sha256": hashlib.sha256(self.output).hexdigest()}],
                        "execution_ok": self.result_ok}}
        if method == "read_artifact":
            assert args["name"] == "output.txt"
            start = args["offset"]
            data = self.output[start:start + args["limit"]]
            return {"attempt_id": attempt_id, "name": "output.txt", "offset": start,
                    "next_offset": start + len(data), "total": len(self.output),
                    "sha256": hashlib.sha256(self.output).hexdigest(),
                    "data_base64": base64.b64encode(data).decode()}
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
    assert store.get(job["id"])["phase"] == "terminal"
    assert store.get(job["id"])["outcome"] == "cancelled"


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


def test_bounded_supervisor_wire_and_error(monkeypatch, tmp_path):
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
            assert path.startswith("/proc/self/fd/") and path.endswith("/control.sock")
        def sendall(self, data):
            self.wire = data
        def makefile(self, mode):
            return io.BytesIO(self.answer)

    fake = Socket()
    monkeypatch.setattr(dispatch.socket, "socket", lambda *_: fake)
    control = SupervisorControl(tmp_path / "control.sock", timeout=2)
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
    assert not store.pending_commands()


def test_verified_result_finalizes_without_human_acceptance(tmp_path):
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    attempt_id = store.get(job["id"])["attempt_id"]
    control.items[attempt_id]["state"] = "stopped"
    control.exit_code = 0
    control.result_ok = True
    dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "terminal" and result["outcome"] == "succeeded"
    assert result["attempt_id"] == attempt_id


def test_post_stop_log_and_artifact_evidence(tmp_path):
    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    attempt_id = store.get(job["id"])["attempt_id"]
    control.items[attempt_id]["state"] = "stopped"
    dispatcher.tick()
    adapter = SupervisorEvidence(dispatcher)
    result = store.get(job["id"])
    manifest = adapter.artifacts(result, attempt_id)
    assert manifest["items"][0]["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    logs = adapter.logs(result, attempt_id, 0, 3)
    assert logs["text"] == "hel" and logs["next_cursor"] == 3
    chunk = adapter.artifact(result, attempt_id, "output.txt", 3, 3)
    assert base64.b64decode(chunk["data_base64"]) == b"lo\n"


class RuntimeForService:
    """Synthetic runtime behind the real supervisor journal/service contract."""

    def __init__(self, root):
        self.root = root
        self.items = {}
        self.starts = 0

    def validate(self, plan):
        assert plan["profile_ref"] == "offline" and plan["workspace_ref"] == "scratch"
        if not plan["payload"].get("argv"):
            raise PlanRejected("invalid synthetic command")

    def launch(self, plan):
        self.starts += 1
        ident = "runtime-" + plan["attempt_id"]
        self.items[plan["attempt_id"]] = {"identity_ok": True, "running": True,
            "stopped": False, "exit_code": None, "runtime_id": ident}
        return ident

    def inspect(self, plan, runtime_id):
        return deepcopy(self.items[plan["attempt_id"]])

    def stop(self, plan, runtime_id):
        item = self.items[plan["attempt_id"]]
        assert item["runtime_id"] == runtime_id
        item.update(running=False, stopped=True, exit_code=143)

    def collect(self, plan, runtime_id):
        import json
        path = self.root / (plan["attempt_id"] + "-manifest.json")
        data = b"hello\n"
        item = self.items[plan["attempt_id"]]
        path.write_text(json.dumps({"attempt_id": plan["attempt_id"],
            "execution_ok": item["exit_code"] == 0,
            "files": [{"path": "output.txt", "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()}]}))
        return str(path)

    def read_artifact(self, plan, runtime_id, name):
        assert name == "output.txt"
        return b"hello\n"


class NoStartRuntime(RuntimeForService):
    """Fail before create; the supervisor alone decides whether absence is proved."""

    def __init__(self, root, *, proved=True):
        super().__init__(root)
        self.proved = proved

    def launch(self, plan):
        self.starts += 1
        raise RuntimeError("workspace setup failed")

    def inspect_no_start(self, plan):
        return self.proved

    def inspect(self, plan, runtime_id):
        raise RuntimeError("runtime not confirmed")


class ServiceBridge:
    def __init__(self, service):
        self.service = service

    def call(self, method, **args):
        try:
            return self.service.dispatch({"method": method, "args": args})
        except Exception as exc:
            raise ControlError(type(exc).__name__) from exc


def test_rejected_launch_tombstone_does_not_stall_next_job(tmp_path):
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    root = tmp_path / "supervisor"
    root.mkdir(mode=0o700)
    runtime = RuntimeForService(root)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(Supervisor(root, runtime))))
    bad = store.submit(spec.model_copy(update={"payload": {}}), principal="owner", key="bad")
    good = store.submit(spec, principal="owner", key="good")
    assert dispatcher.tick()["admitted"] == bad["id"]
    assert dispatcher.tick()["admitted"] == good["id"]
    rejected = store.get(bad["id"])
    assert (rejected["phase"], rejected["outcome"], rejected["reason"]) == (
        "terminal", "failed", "launch_rejected")
    assert rejected["attempts"][-1]["no_start_reason"] == "prelaunch_abandon"
    assert runtime.starts == 0 and not any(op["job_id"] == bad["id"] for op in store.pending_commands())
    assert dispatcher.tick()["processed"] == 1
    assert store.get(good["id"])["visibility"] == "fresh" and runtime.starts == 1
    assert SupervisorEvidence(dispatcher).health()["last_problem"] == {
        "job_id": bad["id"], "code": "launch_rejected"}


def test_lost_rejection_tombstone_reply_keeps_terminal_reason(tmp_path):
    import pytest

    store, spec = setup(tmp_path)
    job = store.submit(spec.model_copy(update={"payload": {}}), principal="owner", key="bad")

    class LostAbandonReply(FakeControl):
        lost = False

        def call(self, method, **args):
            if method == "launch":
                raise ControlError("LaunchRejected")
            result = super().call(method, **args)
            if method == "abandon" and not self.lost:
                self.lost = True
                raise Unavailable("synthetic reply loss")
            return result

    dispatcher = Dispatcher(store, LostAbandonReply())
    dispatcher.tick()
    with pytest.raises(Unavailable):
        dispatcher.tick()
    assert store.get(job["id"])["phase"] == "active"
    dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "terminal" and result["reason"] == "launch_rejected"
    assert result["attempts"][-1]["no_start_reason"] == "prelaunch_abandon"
    assert not store.pending_commands()


def test_identity_conflict_quarantines_only_its_command(tmp_path):
    store, spec = setup(tmp_path)
    first = store.submit(spec, principal="owner", key="first")
    second = store.submit(spec, principal="owner", key="second")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    dispatcher.tick()
    first_active, second_active = store.get(first["id"]), store.get(second["id"])
    store.reattach(first["id"], expected_version=first_active["version"],
                   principal="owner", key="reattach")
    store.cancel(second["id"], expected_version=second_active["version"],
                 principal="owner", key="cancel")
    control.items[first_active["attempt_id"]]["job_id"] = "mismatch"
    dispatcher.tick()
    assert store.get(first["id"])["visibility"] == "unknown"
    assert store.get(first["id"])["reason"] == "identity_conflict"
    assert store.get(second["id"])["outcome"] == "cancelled"
    assert not store.pending_commands()
    assert SupervisorEvidence(dispatcher).health()["last_problem"] == {
        "job_id": first["id"], "code": "identity_conflict"}


def test_transient_supervisor_failure_does_not_make_job_terminal(tmp_path):
    import pytest

    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="first")

    class UnavailableControl(FakeControl):
        def call(self, method, **args):
            if method == "launch":
                raise ControlError("Unavailable")
            return super().call(method, **args)

    dispatcher = Dispatcher(store, UnavailableControl())
    dispatcher.tick()
    with pytest.raises(ControlError, match="Unavailable"):
        dispatcher.tick()
    current = store.get(job["id"])
    assert current["phase"] == "active" and current["outcome"] is None
    assert current["visibility"] == "unknown"
    assert store.pending_commands()[0]["job_id"] == job["id"]


def test_generic_validation_value_error_is_not_a_launch_rejection(tmp_path):
    import pytest
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    root = tmp_path / "supervisor"
    root.mkdir(mode=0o700)

    class UnreadableRuntime(RuntimeForService):
        def validate(self, plan):
            raise ValueError("synthetic malformed daemon response")

    runtime = UnreadableRuntime(root)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(Supervisor(root, runtime))))
    job = store.submit(spec, principal="owner", key="bad")
    dispatcher.tick()
    with pytest.raises(ControlError, match="ValueError"):
        dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "active" and result["outcome"] is None
    assert result["visibility"] == "unknown" and result["reason"] != "launch_rejected"
    assert not result["attempts"][-1]["no_start_reason"]
    assert store.pending_commands()[0]["job_id"] == job["id"]


def test_reservation_full_leaves_launch_retryable_and_runs_later_cancel(tmp_path):
    store, spec = setup(tmp_path)
    first = store.submit(spec, principal="owner", key="first")
    second = store.submit(spec, principal="owner", key="second")

    class CapacityControl(FakeControl):
        freed = False

        def call(self, method, **args):
            if method == "launch" and args["plan"]["job_id"] == second["id"] and not self.freed:
                raise ControlError("ReservationFull")
            result = super().call(method, **args)
            if method == "cancel":
                self.freed = True
            return result

    control = CapacityControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    active = store.get(first["id"])
    store.cancel(first["id"], expected_version=active["version"],
                 principal="owner", key="cancel")
    dispatcher.tick()
    assert store.get(first["id"])["outcome"] == "cancelled"
    assert store.get(second["id"])["phase"] == "active"
    assert any(op["job_id"] == second["id"] for op in store.pending_commands())
    dispatcher.tick()
    assert store.get(second["id"])["visibility"] == "fresh"


def test_real_supervisor_contract_two_jobs_and_verified_evidence(tmp_path):
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = RuntimeForService(supervisor_root)
    bridge = ServiceBridge(SupervisorService(Supervisor(supervisor_root, runtime)))
    dispatcher = Dispatcher(store, bridge)
    first = store.submit(spec, principal="owner", key="first")
    second = store.submit(spec, principal="owner", key="second")
    dispatcher.tick()
    dispatcher.tick()
    dispatcher.tick()
    assert runtime.starts == 2
    assert store.get(first["id"])["attempt_id"] != store.get(second["id"])["attempt_id"]
    attempt_id = store.get(first["id"])["attempt_id"]
    runtime.items[attempt_id].update(running=False, stopped=True, exit_code=0)
    dispatcher.tick()
    assert store.get(first["id"])["outcome"] == "succeeded"
    assert store.get(second["id"])["phase"] == "active"
    adapter = SupervisorEvidence(dispatcher)
    chunk = adapter.artifact(store.get(first["id"]), attempt_id, "output.txt", 0, 6)
    assert base64.b64decode(chunk["data_base64"]) == b"hello\n"


def test_real_supervisor_contract_prelaunch_abandon(tmp_path):
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = RuntimeForService(supervisor_root)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(
        Supervisor(supervisor_root, runtime))))
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher.tick()
    active = store.get(job["id"])
    store.cancel(job["id"], expected_version=active["version"], principal="owner", key="cancel")
    dispatcher.tick()
    assert runtime.starts == 0
    result = store.get(job["id"])
    assert result["phase"] == "terminal" and result["outcome"] == "cancelled"


def test_real_supervisor_no_start_failure_replay_retry_and_artifacts(tmp_path):
    import pytest
    from coordinator import Conflict
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = NoStartRuntime(supervisor_root)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(
        Supervisor(supervisor_root, runtime))))
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    uncertain = store.get(job["id"])
    assert uncertain["phase"] == "active" and uncertain["visibility"] == "unknown"
    assert runtime.starts == 1
    dispatcher.tick()
    result = store.get(job["id"])
    attempt = result["attempts"][-1]
    assert (result["phase"], result["outcome"], result["reason"]) == (
        "terminal", "failed", "workspace_setup_failed")
    assert attempt["runtime_id"] is None and attempt["exit_code"] is None
    assert attempt["no_start_reason"] == "workspace_setup_failed"
    assert not store.pending_commands()
    events = store.replay(job["id"])["events"]
    assert sum(event["kind"] == "no_start_observed" for event in events) == 1
    dispatcher.tick()
    assert store.replay(job["id"])["events"] == events and runtime.starts == 1
    assert SupervisorEvidence(dispatcher).artifacts(result, attempt["id"])["items"] == []
    with pytest.raises(Unavailable, match="log export"):
        SupervisorEvidence(dispatcher).logs(result, attempt["id"], 0, 20)
    retry = store.retry(job["id"], expected_version=result["version"],
                        principal="owner", key="retry")
    assert retry["attempt_id"] != attempt["id"]
    with pytest.raises(Conflict, match="identity mismatch"):
        dispatcher._observe(retry, dispatcher.control.call("inspect", attempt_id=attempt["id"]))


def test_real_supervisor_no_start_sticky_cancel(tmp_path):
    import pytest
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = NoStartRuntime(supervisor_root)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(
        Supervisor(supervisor_root, runtime))))
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    current = store.get(job["id"])
    store.cancel(job["id"], expected_version=current["version"],
                 principal="owner", key="cancel")
    dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "terminal" and result["outcome"] == "cancelled"
    assert result["reason"] == "workspace_setup_failed"
    assert result["attempts"][-1]["no_start_reason"] == "workspace_setup_failed"
    assert runtime.starts == 1
    assert not store.pending_commands()


def test_real_supervisor_ambiguous_create_remains_unknown(tmp_path):
    import pytest
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = NoStartRuntime(supervisor_root, proved=False)
    dispatcher = Dispatcher(store, ServiceBridge(SupervisorService(
        Supervisor(supervisor_root, runtime))))
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "active" and result["visibility"] == "unknown"
    assert result["outcome"] is None and runtime.starts == 1
    assert not any(event["kind"] == "no_start_observed" for event in store.replay(job["id"])["events"])


def test_dispatcher_rejects_mismatched_no_start_evidence(tmp_path):
    import pytest
    from coordinator import Conflict
    from coordinator.supervisor import Supervisor
    from coordinator.supervisor_service import SupervisorService

    store, spec = setup(tmp_path)
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir(mode=0o700)
    runtime = NoStartRuntime(supervisor_root)

    class CorruptBridge(ServiceBridge):
        def call(self, method, **args):
            result = super().call(method, **args)
            if method == "collect":
                return {**result, "runtime_id": "wrong-runtime"}
            return result

    dispatcher = Dispatcher(store, CorruptBridge(SupervisorService(
        Supervisor(supervisor_root, runtime))))
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher.tick()
    with pytest.raises(ControlError, match="OwnershipUnknown"):
        dispatcher.tick()
    dispatcher.tick()
    result = store.get(job["id"])
    assert result["phase"] == "active" and result["outcome"] is None
    assert result["attempts"][-1]["no_start_reason"] is None
    assert result["visibility"] == "unknown" and result["reason"] == "identity_conflict"
    assert not store.pending_commands()


def test_same_evidence_via_api_and_cli(tmp_path, capsys):
    import asyncio
    import json
    import httpx
    from coordinator.api import create_app
    from coordinator import cli

    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    attempt_id = store.get(job["id"])["attempt_id"]
    control.items[attempt_id]["state"] = "stopped"
    dispatcher.tick()
    app = create_app(store, adapter=SupervisorEvidence(dispatcher))
    prefix = f"/v1/jobs/{job['id']}/attempts/{attempt_id}"

    def fetch(path, params=None):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://coordinator") as client:
                response = await client.get(path, params=params)
                assert response.status_code == 200, response.text
                return response.json()
        return asyncio.run(request())

    class Client:
        def logs(self, job_id, attempt, *, cursor, limit):
            return fetch(prefix + "/logs", {"cursor": cursor, "limit": limit})
        def artifacts(self, job_id, attempt):
            return fetch(prefix + "/artifacts")
        def artifact(self, job_id, attempt, artifact_id, *, offset, limit):
            return fetch(prefix + "/artifacts/" + artifact_id,
                         {"offset": offset, "limit": limit})

    api_logs = fetch(prefix + "/logs", {"cursor": 0, "limit": 6})
    args = cli.parser().parse_args(["--socket", "/unused", "--json", "logs",
                                    job["id"], attempt_id, "--limit", "6"])
    assert cli.run(args, Client()) == 0
    assert json.loads(capsys.readouterr().out) == api_logs
    api_manifest = fetch(prefix + "/artifacts")
    args = cli.parser().parse_args(["--socket", "/unused", "--json", "artifacts",
                                    job["id"], attempt_id])
    assert cli.run(args, Client()) == 0
    assert json.loads(capsys.readouterr().out) == api_manifest
    api_chunk = fetch(prefix + "/artifacts/output.txt", {"offset": 0, "limit": 6})
    args = cli.parser().parse_args(["--socket", "/unused", "--json", "artifact",
                                    job["id"], attempt_id, "output.txt", "--limit", "6"])
    assert cli.run(args, Client()) == 0
    assert json.loads(capsys.readouterr().out) == api_chunk


def test_durable_reattach_same_key_after_lost_response(tmp_path):
    import pytest
    from coordinator import Conflict

    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    active = store.get(job["id"])
    attempt_id = active["attempt_id"]
    requested = store.reattach(job["id"], expected_version=active["version"],
                               principal="owner", key="reattach-1")
    assert requested["attempt_id"] == attempt_id
    assert requested["operation_id"]
    dispatcher.tick()  # simulate accepted request whose HTTP response was lost
    replay = store.reattach(job["id"], expected_version=active["version"],
                            principal="owner", key="reattach-1")
    assert replay["attempt_id"] == attempt_id and control.starts == 1
    assert replay["operation_id"] == requested["operation_id"]
    assert replay["outcome"] == "confirmed"
    assert not any(op["kind"] == "reattach" for op in store.pending_commands())
    with pytest.raises(Conflict):
        store.reattach(job["id"], expected_version=active["version"],
                       principal="owner", key="fresh-stale")
    events = store.replay(job["id"])["events"]
    assert sum(event["kind"] == "reattach_requested" for event in events) == 1


def test_reattach_api_replay_and_stale_conflict(tmp_path):
    import asyncio
    import httpx
    from coordinator.api import create_app

    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    dispatcher = Dispatcher(store, FakeControl())
    dispatcher.tick()
    dispatcher.tick()
    active = store.get(job["id"])
    app = create_app(store, adapter=SupervisorEvidence(dispatcher))
    path = f"/v1/jobs/{job['id']}/reattach"
    body = {"expected_version": active["version"], "idempotency_key": "same"}

    def post(payload):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://coordinator") as client:
                return await client.post(path, json=payload)
        return asyncio.run(request())

    first = post(body)
    assert first.status_code == 200 and first.json()["operation"]["state"] == "pending"
    dispatcher.tick()
    replay = post(body)
    assert replay.status_code == 200
    assert replay.json()["operation"]["attempt_id"] == active["attempt_id"]
    assert replay.json()["operation"]["id"] == first.json()["operation"]["id"]
    assert replay.json()["operation"]["state"] == "confirmed"
    assert post(dict(body, idempotency_key="fresh")).status_code == 409
    assert dispatcher.control.starts == 1


def test_lost_reattach_response_keeps_original_attempt_after_retry(tmp_path):
    import asyncio
    import httpx
    from coordinator.api import create_app

    store, spec = setup(tmp_path)
    job = store.submit(spec, principal="owner", key="submit")
    control = FakeControl()
    dispatcher = Dispatcher(store, control)
    dispatcher.tick()
    dispatcher.tick()
    a = store.get(job["id"])
    attempt_a = a["attempt_id"]
    app = create_app(store, adapter=SupervisorEvidence(dispatcher))
    path = f"/v1/jobs/{job['id']}/reattach"
    body = {"expected_version": a["version"], "idempotency_key": "lost-response"}

    def post(payload):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://coordinator") as client:
                return await client.post(path, json=payload)
        return asyncio.run(request())

    first = post(body)
    assert first.status_code == 200
    operation_a = first.json()["operation"]
    control.items[attempt_a]["state"] = "stopped"
    control.exit_code = 1
    control.result_ok = False
    dispatcher.tick()
    failed = store.get(job["id"])
    assert failed["outcome"] == "failed"
    store.retry(job["id"], expected_version=failed["version"],
                principal="owner", key="retry")
    dispatcher.tick()
    attempt_b = store.get(job["id"])["attempt_id"]
    assert attempt_b != attempt_a
    replay = post(body)
    assert replay.status_code == 200
    assert replay.json()["job"]["attempt_id"] == attempt_b
    assert replay.json()["operation"]["attempt_id"] == attempt_a
    assert replay.json()["operation"]["id"] == operation_a["id"]
    assert replay.json()["operation"]["state"] == "confirmed"
    assert post(dict(body, idempotency_key="new-stale")).status_code == 409
    assert control.starts == 2
