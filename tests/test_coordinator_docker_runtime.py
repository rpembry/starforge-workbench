"""Docker command fakes; no socket, sudo, image pull, or live runtime."""

import json
import os
import errno
import base64

import pytest

from coordinator.docker_runtime import DockerRuntime
from coordinator.supervisor import OwnershipUnknown, Supervisor
from coordinator.worker_sdk import WorkerClient


def profile():
    return dict(backend="docker", repository_strategy="per-task-worktree",
                image="example.invalid/reviewed@sha256:" + "a" * 64,
                toolchain="fixture", user=os.getuid(), group=os.getgid(),
                cpus=1, memory_mb=64, pids_limit=32, timeout_seconds=30,
                network="none", mounts=[dict(source="worktree", target="/workspace", read_only=False)])


def plan():
    return {"job_id": "job", "attempt_id": "a" * 32, "incarnation": "inc",
            "worker_type": "command", "profile_ref": "offline", "workspace_ref": "scratch",
            "payload": {"argv": ["sh", "-c", "cat job.json > result.txt"],
                        "input": {"value": 3}, "artifacts": ["result.txt"]},
            "deadline_seconds": 30,
            "orphan_policy": {"mode": "strict", "grace_seconds": 2, "max_orphan_seconds": 0}}


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.item = None
        self.create_count = 0
        self.lose_create_response = False
        self.lose_start_response = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if "info" in argv:
            return json.dumps({"NCPU": 4, "MemTotal": 1024**3}).encode()
        if "image" in argv:
            return json.dumps([{"Id": "image-1", "Config": {"Env": [], "Volumes": None}}]).encode()
        if "create" in argv:
            self.create_count += 1
            labels = {}
            for index, part in enumerate(argv):
                if part == "--label":
                    key, value = argv[index + 1].split("=", 1)
                    labels[key] = value
            self.item = {"Id": "container-1", "Name": "/swb-" + "a" * 32,
                         "Config": {"Labels": labels}, "Mounts": [],
                         "State": {"Status": "created", "Running": False, "ExitCode": 0}}
            for index, part in enumerate(argv):
                if part == "--volume" and argv[index + 1].endswith(":/channel:Z"):
                    self.item["Mounts"].append({"Type": "bind", "Source": argv[index + 1].split(":", 1)[0],
                                                "Destination": "/channel", "RW": True})
            if self.lose_create_response:
                raise TimeoutError("lost Docker create reply")
            return b"container-1\n"
        if "ps" in argv:
            return b"container-1\n" if self.item else b""
        if "inspect" in argv:
            return json.dumps([self.item]).encode()
        if "start" in argv:
            self.item["State"].update(Status="running", Running=True)
            if self.lose_start_response:
                raise TimeoutError("lost Docker start reply")
            return b""
        if "stop" in argv:
            self.item["State"].update(Status="exited", Running=False, ExitCode=143)
            return b""
        if "logs" in argv:
            return b"fixture output\n"
        raise AssertionError(argv)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    if os.getuid() == 0 or os.getgid() == 0:
        pytest.skip("restricted runner needs non-root caller")
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    fake = FakeDocker()
    monkeypatch.setattr("coordinator.docker_runtime.verify_container", lambda item, receipt, p: None)
    return DockerRuntime(root, profiles={"offline": profile()},
                         workspaces={"scratch": "scratch"}, command_fn=fake), fake


def test_restricted_scratch_launch_stop_and_export(runtime):
    adapter, fake = runtime
    adapter.validate(plan())
    runtime_id = adapter.launch(plan())
    assert runtime_id == "container-1" and fake.create_count == 1
    args = next(argv for argv in fake.calls if "create" in argv)
    for flag, value in (("--pull", "never"), ("--network", "none"),
                        ("--cap-drop", "ALL"), ("--restart", "no")):
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args and "--privileged" not in args
    assert not any("/var/run/docker.sock" in args[index + 1]
                   for index, value in enumerate(args[:-1]) if value == "--volume")
    assert adapter.inspect(plan(), runtime_id)["running"]
    assert json.loads((adapter.root / ("a" * 32) / "worktree" / "job.json").read_text()) == {"value": 3}
    (adapter.root / ("a" * 32) / "worktree" / "result.txt").write_text("fixture\n")
    adapter.stop(plan(), runtime_id)
    assert adapter.inspect(plan(), runtime_id)["stopped"]
    manifest = json.loads(open(adapter.collect(plan(), runtime_id)).read())
    assert {item["path"] for item in manifest["files"]} == {"output.txt", "file-0", "exit.json"}


def test_lost_create_response_reconciles_same_resource(runtime, tmp_path):
    adapter, fake = runtime
    fake.lose_create_response = True
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="launch-op")
    assert fake.create_count == 1
    assert supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="launch-op")["state"] == "unknown"
    assert supervisor.reconcile("a" * 32, controller="controller", generation=lease["generation"])["state"] == "stopped"  # created, never started
    assert fake.create_count == 1
    assert supervisor.owner_stop("a" * 32, operation_id="owner-op")["state"] == "stopped"


def test_unapproved_reference_rejected_before_docker(runtime):
    adapter, fake = runtime
    bad = {**plan(), "workspace_ref": "outside"}
    with pytest.raises(ValueError, match="workspace"):
        adapter.validate(bad)
    assert fake.calls == []


def test_protocol_channel_reconnect_and_result_evidence(tmp_path, monkeypatch):
    if os.getuid() == 0 or os.getgid() == 0:
        pytest.skip("restricted runner needs non-root caller")
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    raw = profile()
    raw["mounts"].append(dict(source="scratch", target="/scratch", read_only=False))
    fake = FakeDocker()
    monkeypatch.setattr("coordinator.docker_runtime.verify_container", lambda item, receipt, p: None)
    adapter = DockerRuntime(root, profiles={"offline": raw},
                            workspaces={"scratch": "scratch"}, command_fn=fake)
    spec = {**plan(), "worker_type": "protocol_example", "payload": {"input": {"value": 3}}}
    try:
        try:
            runtime_id = adapter.launch(spec)
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                pytest.skip("sandbox forbids binding Unix sockets")
            raise
        client = WorkerClient(adapter.channels["a" * 32].connect_path,
                              str(root / ("a" * 32) / "scratch" / "sender.json"),
                              job_id="job", attempt_id="a" * 32, incarnation="inc")
        client.send("hello", {"capabilities": []})
        client.send("ready", {})
        (root / ("a" * 32) / "scratch" / "result.json").write_text('{"value":3}\n')
        client.send("artifact", {"path": "result.json"})
        client.send("result", {"ok": True, "summary": "fixture completed"})
        adapter.close_channels()
        adapter.reopen_channels()  # supervisor restart: same inbox, no new container
        fake.item["State"].update(Status="exited", Running=False, ExitCode=0)
        manifest = json.loads(open(adapter.collect(spec, runtime_id)).read())
        assert manifest["execution_ok"] is True
        assert fake.create_count == 1
        assert {entry["path"] for entry in manifest["files"]} == {"output.txt", "file-0", "exit.json"}
        assert not (root / ("a" * 32) / "channel" / "worker-inbox.json").exists()
        assert (root / ("a" * 32) / "host-inbox" / "worker-inbox.json").exists()
    finally:
        adapter.close_channels()


def test_supervisor_collect_commits_exit_and_artifact_evidence(runtime, tmp_path):
    adapter, fake = runtime
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="launch-op")
    (adapter.root / ("a" * 32) / "worktree" / "result.txt").write_text("fixture\n")
    fake.item["State"].update(Status="exited", Running=False, ExitCode=0)
    stopped = supervisor.reconcile("a" * 32, controller="controller", generation=lease["generation"])
    assert stopped["state"] == "stopped"
    evidence = supervisor.collect("a" * 32, controller="controller", generation=lease["generation"])
    assert evidence["exit_code"] == 0 and evidence["result_ok"] is True
    assert evidence["attempt_id"] == "a" * 32 and evidence["observation_seq"] > stopped["observation_seq"]
    assert {entry["path"] for entry in evidence["artifact_manifest"]["files"]} == {
        "output.txt", "file-0", "exit.json"}
    chunk = supervisor.read_artifact("a" * 32, "file-0", offset=0, limit=3)
    assert base64.b64decode(chunk["data_base64"]) == b"fix" and chunk["total"] == 8
    (adapter.root / ("a" * 32) / "worktree" / "result.txt").write_text("changed after collection\n")
    reopened = Supervisor(journal, adapter)
    assert reopened.collect("a" * 32, controller="controller", generation=lease["generation"]) == evidence  # durable replay; no recollect
    exported = adapter.root / ("a" * 32) / "artifacts" / "file-0"
    exported.write_text("tampered\n")
    with pytest.raises(Exception, match="hash changed"):
        supervisor.read_artifact("a" * 32, "file-0")


@pytest.mark.parametrize("stop_mode", ["abandon", "owner", "deadline"])
def test_lost_start_response_still_stops_exact_runtime(runtime, tmp_path, stop_mode):
    adapter, fake = runtime
    fake.lose_start_response = True
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    clock = [1000.0]
    supervisor = Supervisor(journal, adapter, clock=lambda: clock[0])
    lease = supervisor.acquire("controller", lease_seconds=5)
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(plan(), controller="controller", generation=lease["generation"], operation_id="launch-op")
    assert fake.item["State"]["Running"] and supervisor.inspect("a" * 32)["runtime_id"] is None
    if stop_mode == "abandon":
        result = supervisor.abandon(plan(), controller="controller",
                                    generation=lease["generation"], operation_id="launch-op")
    elif stop_mode == "owner":
        result = supervisor.owner_stop("a" * 32, operation_id="owner-op")
    else:
        clock[0] = 1008
        assert supervisor.tick() == ["a" * 32]
        result = supervisor.inspect("a" * 32)
    assert result["state"] == "stopped" and result["runtime_id"] == "container-1"
    assert fake.item["State"]["Running"] is False and fake.create_count == 1


def test_policy_revocation_cannot_prevent_prelaunch_tombstone(runtime, tmp_path):
    adapter, fake = runtime
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    original_profile = adapter.profiles.pop("offline")
    cancelled = supervisor.abandon(plan(), controller="controller",
                                   generation=lease["generation"], operation_id="launch-op")
    assert cancelled["state"] == "stopped" and fake.create_count == 0
    adapter.profiles["offline"] = original_profile
    assert supervisor.launch(plan(), controller="controller",
                             generation=lease["generation"], operation_id="launch-op")["state"] == "stopped"
    assert fake.create_count == 0
    with pytest.raises(ValueError, match="unapproved"):
        adapter.profiles.pop("offline")
        supervisor.launch({**plan(), "attempt_id": "b" * 32}, controller="controller",
                          generation=lease["generation"], operation_id="new-op")
