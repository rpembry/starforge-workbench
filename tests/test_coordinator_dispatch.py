"""Durable relay tests with a fake independent supervisor control boundary."""
from copy import deepcopy
import base64
import hashlib

from coordinator import CoordinatorStore, JobSpec, Limits, Policy
from coordinator.dispatch import ControlError, Dispatcher, SupervisorEvidence


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
                    "state": "stopped", "runtime_id": None, "observation_seq": 1}
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


class ServiceBridge:
    def __init__(self, service):
        self.service = service

    def call(self, method, **args):
        try:
            return self.service.dispatch({"method": method, "args": args})
        except Exception as exc:
            raise ControlError(type(exc).__name__) from exc


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
