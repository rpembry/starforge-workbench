"""Docker command fakes; no socket, sudo, image pull, or live runtime."""

import json
import os
import errno
import base64
import subprocess
import sqlite3

import pytest

from coordinator.docker_runtime import DockerRuntime
from coordinator.supervisor import Conflict, OwnershipUnknown, Supervisor
from coordinator.worker_sdk import WorkerClient
from starforge_workbench.docker_worker import WorkerError


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
        self.lose_rm_response = False

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
            self.item = {"Id": "container-1", "Name": "/" + argv[argv.index("--name") + 1],
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
        if "rm" in argv:
            self.item = None
            if self.lose_rm_response:
                self.lose_rm_response = False
                raise TimeoutError("lost Docker remove reply")
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
    receipt = json.loads((adapter.root / ("a" * 32) / "runtime.json").read_text())
    assert receipt["container_id"] == "container-1"
    receipt_path = adapter.root / ("a" * 32) / "runtime.json"
    receipt["container_id"] = "different-container"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(WorkerError, match="runtime plan or incarnation mismatch"):
        adapter.inspect(plan(), "container-1")
    receipt["container_id"] = "container-1"
    receipt_path.write_text(json.dumps(receipt))
    assert supervisor.owner_stop("a" * 32, operation_id="owner-op")["state"] == "stopped"
    (adapter.root / ("a" * 32) / "worktree" / "result.txt").write_text("retained\n")
    supervisor.collect("a" * 32, controller="controller",
                       generation=lease["generation"])
    reviewed = supervisor.review("a" * 32)
    fake.lose_rm_response = True
    with pytest.raises(TimeoutError, match="lost Docker remove reply"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="archive-after-adoption")
    assert supervisor.reconcile("a" * 32, controller="controller",
                                generation=lease["generation"])["state"] == "stopped"
    supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                       operation_id="archive-after-adoption")
    restarted = Supervisor(journal, adapter)
    assert restarted.reconcile("a" * 32, controller="controller",
                               generation=lease["generation"])["state"] == "stopped"


def test_unapproved_reference_rejected_before_docker(runtime):
    adapter, fake = runtime
    bad = {**plan(), "workspace_ref": "outside"}
    with pytest.raises(ValueError, match="workspace"):
        adapter.validate(bad)
    assert fake.calls == []


def test_default_host_reservation_blocks_second_allocation(runtime, tmp_path):
    adapter, fake = runtime
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    supervisor.launch(plan(), controller="controller", generation=lease["generation"],
                      operation_id="launch-one")
    second = {**plan(), "attempt_id": "b" * 32, "incarnation": "different"}
    with pytest.raises(Conflict, match="reservation full"):
        supervisor.launch(second, controller="controller", generation=lease["generation"],
                          operation_id="launch-two")
    assert fake.create_count == 1
    with pytest.raises(KeyError):
        supervisor.inspect("b" * 32)


def test_approved_git_worktree_exports_patch_and_retains_all_work(runtime, tmp_path):
    adapter, fake = runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repository), *args],
                              check=True, capture_output=True,
                              env={**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                                   "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}).stdout
    git("init", "-q")
    (repository / "input.txt").write_text("before\n")
    git("add", "input.txt")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    revision = git("rev-parse", "HEAD").decode().strip()
    adapter.workspaces["reviewed_git"] = {"kind": "git_worktree", "repository": str(repository),
                                           "revision": revision}
    spec = {**plan(), "workspace_ref": "reviewed_git", "payload": {"argv": ["true"]}}
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    runtime_id = supervisor.launch(spec, controller="controller", generation=lease["generation"],
                                   operation_id="launch")["runtime_id"]
    worktree = adapter.root / ("a" * 32) / "worktree"
    assert git("worktree", "list", "--porcelain").decode().find(str(worktree)) >= 0
    assert (worktree / "input.txt").read_text() == "before\n"
    (worktree / "input.txt").write_text("after\n")
    fake.item["State"].update(Status="exited", Running=False, ExitCode=0)
    manifest = json.loads(open(adapter.collect(spec, runtime_id)).read())
    names = {entry["path"] for entry in manifest["files"]}
    assert {"changes.patch", "status.txt", "output.txt", "exit.json"} <= names
    assert b"+after" in adapter.read_artifact(spec, runtime_id, "changes.patch")
    assert (worktree / "input.txt").read_text() == "after\n"
    assert (adapter.root / ("a" * 32) / "runtime.json").exists()
    supervisor.reconcile("a" * 32, controller="controller", generation=lease["generation"])
    supervisor.collect("a" * 32, controller="controller", generation=lease["generation"])
    reviewed = supervisor.review("a" * 32)
    archived = supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                                  operation_id="archive")
    assert archived["phase"] == "complete"
    assert (adapter.root / ("a" * 32) / "archive" / "worktree" / "input.txt").read_text() == "after\n"
    assert str(worktree) not in git("worktree", "list", "--porcelain").decode()


def test_partial_git_setup_retains_receipt_and_never_creates_container(runtime, tmp_path, monkeypatch):
    adapter, fake = runtime
    repository = tmp_path / "repository"
    repository.mkdir()
    revision = "a" * 40
    adapter.workspaces["reviewed_git"] = {"kind": "git_worktree", "repository": str(repository),
                                           "revision": revision}
    def interrupted_git(repo, *args):
        if args[:1] == ("rev-parse",):
            return (revision + "\n").encode()
        if args[:1] == ("config",):
            return b""
        assert args[:3] == ("worktree", "add", "--detach")
        (adapter.root / ("a" * 32) / "worktree").mkdir()
        (adapter.root / ("a" * 32) / "worktree" / "partial.txt").write_text("reviewed partial bytes\n")
        raise WorkerError("interrupted worktree setup")
    monkeypatch.setattr("coordinator.docker_runtime.git", interrupted_git)
    spec = {**plan(), "workspace_ref": "reviewed_git"}
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    with pytest.raises(OwnershipUnknown, match="launch response unknown"):
        supervisor.launch(spec, controller="controller", generation=lease["generation"],
                          operation_id="partial-launch")
    receipt = json.loads((adapter.root / ("a" * 32) / "runtime.json").read_text())
    assert receipt["phase"] == "workspace_pending" and receipt["revision"] == revision
    assert (adapter.root / ("a" * 32) / "worktree").exists()
    assert not any("create" in argv for argv in fake.calls)
    assert adapter.inspect_no_start(spec) is True
    assert supervisor.reconcile("a" * 32, controller="controller",
                                generation=lease["generation"])["no_start_reason"] == "workspace_setup_failed"
    evidence = supervisor.collect("a" * 32, controller="controller", generation=lease["generation"])
    assert evidence["no_start"] is True and "artifact_manifest" not in evidence
    reviewed = supervisor.review("a" * 32)
    assert reviewed["evidence_kind"] == "no_start_retention"
    partial = adapter.root / ("a" * 32) / "worktree" / "partial.txt"
    partial.write_text("changed after review\n")
    with pytest.raises(WorkerError, match="reviewed source changed"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="retain-partial")
    partial.write_text("reviewed partial bytes\n")
    import coordinator.no_start_retention as retention
    original_copy = retention._copy_reviewed
    def interrupted_copy(attempt, target, entries):
        (target / "worktree").mkdir(exist_ok=True)
        (target / "worktree" / "partial.txt").write_text("incomplete copy\n")
        raise TimeoutError("copy interrupted")
    monkeypatch.setattr(retention, "_copy_reviewed", interrupted_copy)
    with pytest.raises(TimeoutError, match="copy interrupted"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="retain-partial")
    assert json.loads((adapter.root / ("a" * 32) / "no-start-retention.json").read_text())["phase"] == "retaining"
    monkeypatch.setattr(retention, "_copy_reviewed", original_copy)
    retained = supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                                  operation_id="retain-partial")
    assert retained["evidence_kind"] == "no_start_retention" and retained["phase"] == "complete"
    assert (adapter.root / ("a" * 32) / "archive" / "no-start" / "worktree" /
            "partial.txt").read_text() == "reviewed partial bytes\n"
    assert partial.read_text() == "reviewed partial bytes\n"
    assert supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                              operation_id="retain-partial") == retained
    with pytest.raises(Conflict, match="retention operation identity"):
        supervisor.archive("a" * 32, review_sha256="0" * 64,
                           operation_id="retain-partial")
    original_command = adapter.command
    def daemon_unavailable(argv, **kwargs):
        if "ps" in argv:
            raise WorkerError("daemon unavailable")
        return original_command(argv, **kwargs)
    adapter.command = daemon_unavailable
    with pytest.raises(WorkerError, match="daemon unavailable"):
        adapter.inspect_no_start(spec)
    adapter.command = original_command
    receipt["phase"] = "create_pending"
    (adapter.root / ("a" * 32) / "runtime.json").write_text(json.dumps(receipt))
    assert adapter.inspect_no_start(spec) is False


def test_no_start_retention_keeps_real_git_administration(runtime, tmp_path, monkeypatch):
    adapter, fake = runtime
    repository = tmp_path / "source-repository"
    repository.mkdir()
    def git_local(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repository), *args],
                              check=True, capture_output=True,
                              env={**os.environ, "GIT_AUTHOR_NAME": "Fixture",
                                   "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                                   "GIT_COMMITTER_NAME": "Fixture",
                                   "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}).stdout
    git_local("init", "-q")
    (repository / "input.txt").write_text("before\n")
    git_local("add", "input.txt")
    git_local("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    revision = git_local("rev-parse", "HEAD").decode().strip()
    adapter.workspaces["reviewed_git"] = {"kind": "git_worktree", "repository": str(repository),
                                           "revision": revision}
    import coordinator.docker_runtime as docker_module
    original_git = docker_module.git
    def interrupted_after_add(repo, *args):
        result = original_git(repo, *args)
        if args[:3] == ("worktree", "add", "--detach"):
            raise WorkerError("lost worktree add response")
        return result
    monkeypatch.setattr(docker_module, "git", interrupted_after_add)
    spec = {**plan(), "workspace_ref": "reviewed_git"}
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    with pytest.raises(OwnershipUnknown):
        supervisor.launch(spec, controller="controller", generation=lease["generation"],
                          operation_id="lost-worktree-reply")
    original = adapter.root / ("a" * 32) / "worktree"
    assert str(original) in git_local("worktree", "list", "--porcelain").decode()
    assert supervisor.reconcile("a" * 32, controller="controller",
                                generation=lease["generation"])["no_start_reason"] == "workspace_setup_failed"
    supervisor.collect("a" * 32, controller="controller", generation=lease["generation"])
    reviewed = supervisor.review("a" * 32)
    retained = supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                                  operation_id="retain-real-git")
    assert retained["phase"] == "complete"
    assert (original / "input.txt").read_text() == "before\n"
    assert str(original) in git_local("worktree", "list", "--porcelain").decode()
    assert (adapter.root / ("a" * 32) / "archive" / "no-start" / "worktree" /
            "input.txt").read_text() == "before\n"
    assert fake.create_count == 0


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
        assert adapter.protocol_ready(spec, runtime_id) is False
        client.send("hello", {"capabilities": []})
        client.send("ready", {})
        assert adapter.protocol_ready(spec, runtime_id) is True
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


def test_owner_review_archive_retains_exact_scratch_bytes(runtime, tmp_path):
    adapter, fake = runtime
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    supervisor = Supervisor(journal, adapter)
    lease = supervisor.acquire("controller")
    supervisor.launch(plan(), controller="controller", generation=lease["generation"],
                      operation_id="launch")
    attempt = adapter.root / ("a" * 32)
    (attempt / "worktree" / "result.txt").write_text("retained\n")
    (attempt / "scratch" / "note.txt").write_text("reviewed\n")
    fake.item["State"].update(Status="exited", Running=False, ExitCode=0)
    supervisor.reconcile("a" * 32, controller="controller", generation=lease["generation"])
    supervisor.collect("a" * 32, controller="controller", generation=lease["generation"])
    reviewed = supervisor.review("a" * 32)
    with pytest.raises(WorkerError, match="review identity or digest"):
        supervisor.archive("a" * 32, review_sha256="0" * 64, operation_id="invalid-archive")
    (attempt / "scratch" / "note.txt").write_text("changed after review\n")
    with pytest.raises(WorkerError, match="work changed after review"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="changed-archive")
    assert fake.item is not None
    (attempt / "scratch" / "note.txt").write_text("reviewed\n")
    fake.lose_rm_response = True
    with pytest.raises(TimeoutError, match="lost Docker remove reply"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="archive")
    assert fake.item is None
    assert json.loads((attempt / "disposition.json").read_text())["phase"] == "archiving"
    (attempt / "scratch" / "note.txt").write_text("changed after removal\n")
    with pytest.raises(OwnershipUnknown, match="runtime observation unavailable"):
        supervisor.reconcile("a" * 32, controller="controller",
                             generation=lease["generation"])
    assert supervisor.inspect("a" * 32)["state"] == "unknown"
    (attempt / "scratch" / "note.txt").write_text("reviewed\n")
    assert supervisor.reconcile("a" * 32, controller="controller",
                                generation=lease["generation"])["state"] == "stopped"
    # Recover journals left unknown by the older lost-reply/reconcile sequence.
    with sqlite3.connect(journal / "supervisor.sqlite3") as db:
        db.execute("UPDATE attempts SET state='unknown' WHERE id=?", ("a" * 32,))
    recovering = Supervisor(journal, adapter)
    assert recovering.recover_startup() == ["a" * 32]
    assert recovering.inspect("a" * 32)["state"] == "stopped"
    result = recovering.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                                operation_id="archive")
    assert result["phase"] == "complete"
    assert supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                              operation_id="archive") == result
    assert (attempt / "archive" / "worktree" / "result.txt").read_text() == "retained\n"
    assert (attempt / "archive" / "scratch" / "note.txt").read_text() == "reviewed\n"
    assert (attempt / "artifacts" / "manifest.json").exists()
    assert not (attempt / "worktree").exists()
    assert fake.item is None
    assert supervisor.reconcile("a" * 32, controller="controller",
                                generation=lease["generation"])["state"] == "stopped"
    restarted = Supervisor(journal, adapter)
    assert restarted.reconcile("a" * 32, controller="controller",
                               generation=lease["generation"])["state"] == "stopped"
    second = {**plan(), "attempt_id": "b" * 32, "incarnation": "next"}
    assert restarted.launch(second, controller="controller", generation=lease["generation"],
                            operation_id="launch-second")["state"] == "running"
    assert fake.create_count == 2  # archived stopped allocation released host reservation
    (attempt / "archive" / "scratch" / "note.txt").write_text("tampered\n")
    with pytest.raises(WorkerError, match="archived content changed"):
        supervisor.archive("a" * 32, review_sha256=reviewed["review_sha256"],
                           operation_id="archive")


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
