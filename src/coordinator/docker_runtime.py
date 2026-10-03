"""Host-owned scratch/Git Docker adapters for the supervisor protocol.

This reuses the established runner's restricted Docker create/verify functions.
It never adopts legacy receipts, pulls an image, or accepts host paths from a job.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from starforge_workbench.docker_worker import (
    MAX_ARTIFACT, WorkerError, atomic, command, create_argv, docker_prefix,
    environment_digest, git, private_directory, verify_container, verify_worktree,
)
from starforge_workbench.execution import parse_profile

from .store import _json
from .worker_channel import WorkerChannel
from .worker_protocol import WorkerInbox


LABEL = "io.starforge.coordinator"


def protocol_execution_result(exit_code, inbox_state):
    """No success from a lost result, readiness, or artifact declaration gap."""
    if exit_code is None:
        return None
    if exit_code != 0:
        return False
    result = inbox_state["result"]
    if result is not None and result["data"]["ok"] is False:
        return False
    if not inbox_state["ready"] or result is None or not inbox_state["artifacts_complete"]:
        return None
    return True


class DockerRuntime:
    """One local Docker host, with approved immutable refs supplied by operator.

    `profiles` maps names to already reviewed Docker profile mappings.
    `workspaces` maps names to `scratch` or an operator-approved Git repository
    and full commit. `worker_types` is a set of
    registered host adapters: ordinary `command` and fixed `protocol_example`.
    """

    def __init__(self, state_root, *, profiles: dict, workspaces: dict,
                 worker_types=frozenset({"command", "protocol_example"}),
                 host_budget=None, command_fn=command):
        self.root = private_directory(state_root)
        self.profiles = {}
        for name, raw in profiles.items():
            profile = parse_profile(raw)
            if profile.backend != "docker":
                raise ValueError("coordinator requires approved Docker profiles")
            if profile.user != os.getuid() or profile.group != os.getgid():
                raise ValueError("Docker profile UID/GID must match non-root host caller")
            self.profiles[name] = profile
        self.workspaces = dict(workspaces)
        if host_budget is None:
            # Safe single-allocation default until the operator assigns an
            # explicit slice of host capacity to this supervisor.
            host_budget = {"max_active": 1,
                           "cpu_millis": max((math.ceil(p.cpus * 1000) for p in self.profiles.values()), default=0),
                           "memory_mb": max((p.memory_mb for p in self.profiles.values()), default=0)}
        if (not isinstance(host_budget, dict) or set(host_budget) !=
                {"max_active", "cpu_millis", "memory_mb"} or
                any(type(host_budget[key]) is not int or host_budget[key] < 0 for key in host_budget) or
                host_budget["max_active"] < 1):
            raise ValueError("invalid host reservation budget")
        self.host_budget = dict(host_budget)
        self.worker_types = frozenset(worker_types)
        self.command = command_fn
        self.docker = docker_prefix(False)  # no implicit sudo or remote context
        self.channels = {}

    def validate(self, plan):
        if plan["profile_ref"] not in self.profiles:
            raise ValueError("unapproved Docker profile")
        binding = self._workspace_binding(plan["workspace_ref"])
        if binding["kind"] == "git_worktree":
            self._verify_git_source(binding)
        request = self.reservation(plan)
        if (request["cpu_millis"] > self.host_budget["cpu_millis"] or
                request["memory_mb"] > self.host_budget["memory_mb"]):
            raise WorkerError("approved profile exceeds supervisor host reservation")
        capacity = json.loads(self.command(self.docker + ["info", "--format", "{{json .}}"] ))
        if (self.host_budget["cpu_millis"] > capacity["NCPU"] * 1000 or
                self.host_budget["memory_mb"] * 1024 * 1024 > capacity["MemTotal"]):
            raise WorkerError("supervisor reservation exceeds daemon host capacity")
        kind = plan["worker_type"]
        if kind not in {"command", "protocol_example"} or kind not in self.worker_types:
            raise ValueError("unregistered worker adapter")
        if not re.fullmatch(r"[0-9a-f]{32}", plan["attempt_id"]):
            raise ValueError("invalid attempt identity")
        payload = plan["payload"]
        if kind == "protocol_example":
            if set(payload) != {"input"} or not isinstance(payload["input"], dict):
                raise ValueError("protocol example requires bounded input only")
            if len(_json(payload["input"]).encode()) > 65_536:
                raise ValueError("protocol input too large")
            if not any(m.source == "scratch" for m in self.profiles[plan["profile_ref"]].mounts):
                raise ValueError("protocol example needs approved scratch mount")
            return
        if set(payload) - {"argv", "input", "artifacts"}:
            raise ValueError("unsupported command payload")
        argv = payload.get("argv")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128 or
                any(not isinstance(value, str) or not value or "\x00" in value or
                    len(value.encode()) > 4096 for value in argv)):
            raise ValueError("invalid command argument vector")
        if sum(len(value.encode()) for value in argv) > 16_384:
            raise ValueError("command argument vector too large")
        declared = payload.get("artifacts", [])
        if not isinstance(declared, list) or len(declared) > 32:
            raise ValueError("invalid artifact list")
        for name in declared:
            if (not isinstance(name, str) or not name or len(name) > 256 or
                    Path(name).is_absolute() or "\x00" in name or
                    any(part in {".", "..", ".git"} for part in Path(name).parts)):
                raise ValueError("unsafe artifact path")
        if "input" in payload and (not isinstance(payload["input"], dict) or
                                   len(_json(payload["input"]).encode()) > 65_536):
            raise ValueError("invalid bounded input")

    def reservation(self, plan):
        profile = self.profiles[plan["profile_ref"]]
        return {"cpu_millis": math.ceil(profile.cpus * 1000),
                "memory_mb": profile.memory_mb}

    def _workspace_binding(self, reference):
        binding = self.workspaces.get(reference)
        if binding == "scratch":
            return {"kind": "scratch"}
        if (not isinstance(binding, dict) or set(binding) != {"kind", "repository", "revision"} or
                binding["kind"] != "git_worktree" or
                not isinstance(binding["repository"], str) or
                not isinstance(binding["revision"], str) or
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", binding["revision"])):
            raise ValueError("unsupported or unapproved workspace adapter")
        repository = Path(binding["repository"])
        if (not repository.is_absolute() or repository.is_symlink() or
                not repository.is_dir() or repository.resolve() != repository or
                repository == self.root or repository in self.root.parents or self.root in repository.parents):
            raise ValueError("Git workspace repository must be an isolated real path")
        return {"kind": "git_worktree", "repository": str(repository),
                "revision": binding["revision"]}

    @staticmethod
    def _verify_git_source(binding):
        repo = binding["repository"]
        resolved = git(repo, "rev-parse", "--verify", "--end-of-options",
                       binding["revision"] + "^{commit}").decode().strip()
        if resolved != binding["revision"]:
            raise WorkerError("approved Git revision changed or is not a full commit")
        config = git(repo, "config", "--name-only", "--list").decode().splitlines()
        if any(key.startswith("filter.") for key in config):
            raise WorkerError("repositories with content filters are unsupported")

    def _receipt_path(self, attempt_id):
        return self.root / attempt_id / "runtime.json"

    def _read(self, attempt_id):
        path = self._receipt_path(attempt_id)
        if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or
                path.stat().st_mode & 0o077):
            raise WorkerError("private runtime receipt unavailable")
        receipt = json.loads(path.read_text())
        if receipt["attempt_id"] != attempt_id or receipt["attempt_path"] != str(path.parent):
            raise WorkerError("runtime receipt identity mismatch")
        return receipt

    def _write(self, receipt):
        atomic(Path(receipt["attempt_path"]) / "runtime.json", receipt)

    def _inspect(self, receipt):
        ids = self.command(self.docker + ["ps", "-aq", "--no-trunc", "--filter",
                                          "label=" + LABEL + ".attempt=" + receipt["attempt_id"]]).decode().split()
        if not ids:
            return None
        if len(ids) != 1:
            raise WorkerError("ambiguous attempt containers")
        item = json.loads(self.command(self.docker + ["inspect", ids[0]]))[0]
        labels = item["Config"].get("Labels") or {}
        expected = {LABEL + ".attempt": receipt["attempt_id"],
                    LABEL + ".token": receipt["token"],
                    LABEL + ".incarnation": receipt["incarnation"],
                    LABEL + ".plan": receipt["plan_hash"]}
        if (any(labels.get(key) != value for key, value in expected.items()) or
                item["Name"].lstrip("/") != receipt["container_name"] or
                receipt["container_id"] and item["Id"] != receipt["container_id"]):
            raise WorkerError("container ownership mismatch")
        if receipt["worker_type"] == "protocol_example":
            channel = [mount for mount in item["Mounts"] if mount["Destination"] == "/channel"]
            if (len(channel) != 1 or channel[0]["Type"] != "bind" or
                    channel[0]["Source"] != str(Path(receipt["attempt_path"]) / "channel") or
                    not channel[0]["RW"]):
                raise WorkerError("worker channel mount mismatch")
            item = {**item, "Mounts": [mount for mount in item["Mounts"] if mount["Destination"] != "/channel"]}
        verify_container(item, receipt, self.profiles[receipt["profile_ref"]])
        return item

    def launch(self, plan):
        self.validate(plan)
        binding = self._workspace_binding(plan["workspace_ref"])
        profile = self.profiles[plan["profile_ref"]]
        capacity = json.loads(self.command(self.docker + ["info", "--format", "{{json .}}"] ))
        if profile.cpus > capacity["NCPU"] or profile.memory_mb * 1024 * 1024 > capacity["MemTotal"]:
            raise WorkerError("requested resources exceed daemon host capacity")
        image = json.loads(self.command(self.docker + ["image", "inspect", profile.image]))[0]
        if image["Config"].get("Volumes"):
            raise WorkerError("image-declared volumes unsupported")
        env_hash = environment_digest(image["Config"].get("Env"), image=True)
        attempt = self.root / plan["attempt_id"]
        attempt.mkdir(mode=0o700)  # existing attempt is reconciliation, never a new launch
        workspace = attempt / "worktree"  # reuse existing restricted mount verifier
        (attempt / "scratch").mkdir(mode=0o700)
        (attempt / "git-mask").write_text("No container Git administration.\n")
        (attempt / "git-mask").chmod(0o444)
        protocol = plan["worker_type"] == "protocol_example"
        if protocol:
            self.channels[plan["attempt_id"]] = WorkerChannel(
                attempt, job_id=plan["job_id"], attempt_id=plan["attempt_id"],
                incarnation=plan["incarnation"])
        plan_hash = hashlib.sha256(_json(plan).encode()).hexdigest()
        worker_argv = (["python", "/opt/coordinator-worker.py", plan["job_id"],
                        plan["attempt_id"], plan["incarnation"]]
                       if protocol else plan["payload"]["argv"])
        receipt = {"attempt_id": plan["attempt_id"], "attempt_path": str(attempt),
                   "incarnation": plan["incarnation"], "plan_hash": plan_hash,
                   "profile_ref": plan["profile_ref"], "worker_type": plan["worker_type"],
                   "job_id": plan["job_id"], "worktree": str(workspace),
                   "container_name": "swb-" + plan["attempt_id"],
                   "container_id": None, "token": os.urandom(16).hex(),
                   "image_id": image["Id"], "environment_sha256": env_hash,
                   "argv": worker_argv, "artifact_paths": plan["payload"].get("artifacts", []),
                   "workspace_kind": binding["kind"],
                   "phase": "workspace_pending"}
        if binding["kind"] == "git_worktree":
            receipt.update(repository=binding["repository"], revision=binding["revision"])
        self._write(receipt)  # durable receipt before host worktree allocation
        if binding["kind"] == "git_worktree":
            git(binding["repository"], "worktree", "add", "--detach", str(workspace),
                binding["revision"])
            verify_worktree(receipt)
        else:
            workspace.mkdir(mode=0o700)
        if "input" in plan["payload"]:
            fd = os.open(workspace / "job.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, "w") as stream:
                stream.write(_json(plan["payload"]["input"]) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        receipt["phase"] = "create_pending"
        self._write(receipt)  # durable ownership token before Docker create
        argv = create_argv(self.docker, receipt, profile)
        argv[argv.index("--entrypoint"):argv.index("--entrypoint")] = [
            "--label", LABEL + ".attempt=" + receipt["attempt_id"],
            "--label", LABEL + ".token=" + receipt["token"],
            "--label", LABEL + ".incarnation=" + receipt["incarnation"],
            "--label", LABEL + ".plan=" + receipt["plan_hash"]]
        if protocol:
            argv[argv.index("--entrypoint"):argv.index("--entrypoint")] = [
                "--volume", str(attempt / "channel") + ":/channel:Z"]
        try:
            container_id = self.command(argv).decode().strip()
            receipt["container_id"] = container_id
            receipt["phase"] = "created"
            self._write(receipt)
            item = self._inspect(receipt)
            if item is None:
                raise WorkerError("created container not observable")
            self.command(self.docker + ["start", container_id])
            receipt["phase"] = "start_response_received"
            self._write(receipt)
            return container_id
        except Exception:
            # Leave receipt and work intact for exact reconciliation/owner stop.
            raise

    def inspect(self, plan, runtime_id):
        observed = self.inspect_optional(plan, runtime_id)
        if observed is None:
            raise WorkerError("owned container absent without exit evidence")
        return observed

    def inspect_optional(self, plan, runtime_id):
        """Exact ownership check, returning None only for verified absence."""
        receipt = self._read(plan["attempt_id"])
        if (receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest() or
                receipt["incarnation"] != plan["incarnation"] or
                runtime_id and receipt["container_id"] and runtime_id != receipt["container_id"]):
            raise WorkerError("runtime plan or incarnation mismatch")
        item = self._inspect(receipt)
        if item is None:
            return None
        status = item["State"]["Status"]
        return {"identity_ok": True, "running": bool(item["State"]["Running"]),
                "stopped": status in {"exited", "dead", "created"},
                "exit_code": None if status == "created" else item["State"]["ExitCode"],
                "runtime_id": item["Id"]}

    def inspect_no_start(self, plan):
        """Positive precreate proof; an unknown create outcome never qualifies."""
        receipt = self._read(plan["attempt_id"])
        if (receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest() or
                receipt["incarnation"] != plan["incarnation"] or
                receipt["job_id"] != plan["job_id"]):
            raise WorkerError("runtime plan or incarnation mismatch")
        if receipt["phase"] != "workspace_pending" or receipt["container_id"] is not None:
            return False
        return self._inspect(receipt) is None

    def review(self, plan, runtime_id):
        from .disposition import review
        return review(self, plan, runtime_id)

    def archive(self, plan, runtime_id, review_sha256, operation_id):
        from .disposition import archive
        return archive(self, plan, runtime_id, review_sha256, operation_id)

    def inspect_archived(self, plan, runtime_id):
        """Positive disposed-resource proof for repeated supervisor reconcile."""
        from starforge_workbench.worker_disposition import read_private
        receipt = self._read(plan["attempt_id"])
        if (receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest() or
                receipt["incarnation"] != plan["incarnation"] or
                receipt["job_id"] != plan["job_id"] or
                receipt["container_id"] != runtime_id):
            raise WorkerError("archived attempt identity mismatch")
        path = Path(receipt["attempt_path"]) / "disposition.json"
        if not path.exists():
            return False
        disposition = read_private(path)
        if disposition.get("phase") != "complete" or disposition.get("runtime_id") != runtime_id:
            return False
        self.archive(plan, runtime_id, disposition["review_sha256"], disposition["operation_id"])
        if self._inspect(receipt) is not None:
            raise WorkerError("archived container reappeared")
        return True

    def stop(self, plan, runtime_id):
        observed = self.inspect(plan, runtime_id)
        if not observed["stopped"]:
            self.command(self.docker + ["stop", "--time", "5", observed["runtime_id"]], timeout=15)
            if not self.inspect(plan, observed["runtime_id"])["stopped"]:
                raise WorkerError("stop not confirmed")

    def protocol_ready(self, plan, runtime_id):
        """Read only host-acknowledged, contiguous readiness for this exact worker."""
        if plan["worker_type"] != "protocol_example":
            raise ValueError("readiness applies only to protocol workers")
        receipt = self._read(plan["attempt_id"])
        if (receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest() or
                receipt["incarnation"] != plan["incarnation"] or
                receipt["container_id"] != runtime_id):
            raise WorkerError("runtime identity changed before readiness")
        inbox = WorkerInbox(Path(receipt["attempt_path"]) / "host-inbox",
                            job_id=receipt["job_id"], attempt_id=receipt["attempt_id"],
                            incarnation=receipt["incarnation"])
        return bool(inbox.state["hello"] and inbox.state["ready"] and not inbox.state["gaps"])

    def reopen_channels(self):
        """Restore only exact coordinator protocol receipts after service restart."""
        for path in self.root.iterdir():
            if not path.is_dir() or path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", path.name):
                continue
            if not (path / "runtime.json").exists():
                continue
            receipt = self._read(path.name)
            if receipt.get("worker_type") == "protocol_example":
                self.channels[path.name] = WorkerChannel(
                    path, job_id=receipt["job_id"], attempt_id=path.name,
                    incarnation=receipt["incarnation"])

    def close_channels(self):
        for channel in self.channels.values():
            channel.close()
        self.channels.clear()

    def collect(self, plan, runtime_id):
        """Export bounded evidence after positive stop; retain original workspace."""
        observed = self.inspect(plan, runtime_id)
        if not observed["stopped"]:
            raise WorkerError("cannot collect a running attempt")
        receipt = self._read(plan["attempt_id"])
        attempt = Path(receipt["attempt_path"])
        output = attempt / "artifacts"
        output.mkdir(mode=0o700, exist_ok=True)
        if output.is_symlink():
            raise WorkerError("artifact directory changed")
        files = []
        def save(name, data):
            if len(data) > MAX_ARTIFACT:
                raise WorkerError("artifact exceeds byte limit")
            target = output / name
            if target.is_symlink():
                raise WorkerError("artifact path changed")
            fd, path = tempfile.mkstemp(prefix=".artifact-", dir=output)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(path, target)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
            files.append({"path": name, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        save("output.txt", self.command(self.docker + ["logs", "--tail", "1000", runtime_id],
                                        limit=MAX_ARTIFACT, combined=True))
        protocol = receipt["worker_type"] == "protocol_example"
        inbox = (WorkerInbox(attempt / "host-inbox", job_id=receipt["job_id"],
                             attempt_id=receipt["attempt_id"], incarnation=receipt["incarnation"])
                 if protocol else None)
        declarations = inbox.state["artifacts"] if inbox else receipt["artifact_paths"]
        source = attempt / ("scratch" if protocol else "worktree")
        if receipt.get("workspace_kind") == "git_worktree":
            verify_worktree(receipt)
            changed = git(receipt["worktree"], "diff", "--name-only", "-z", "HEAD").split(b"\0")
            total_changed = 0
            for name in changed:
                if not name:
                    continue
                path = Path(receipt["worktree"]) / os.fsdecode(name)
                if path.is_symlink() or not path.exists():
                    continue
                if not path.is_file():
                    raise WorkerError("special changed file; retain worktree for review")
                total_changed += path.stat().st_size
                if total_changed > MAX_ARTIFACT:
                    raise WorkerError("changed Git files exceed export limit")
            save("changes.patch", git(receipt["worktree"], "diff", "--binary",
                                      "--no-ext-diff", "--no-textconv", "HEAD"))
            save("status.txt", git(receipt["worktree"], "status", "--porcelain",
                                   "--untracked-files=all", "--ignored"))
        total = 0
        for index, name in enumerate(declarations):
            path = source / name
            if path.resolve() != path.absolute() or not path.is_file() or path.is_symlink():
                raise WorkerError("declared artifact missing or unsafe")
            size = path.stat().st_size
            total += size
            if size > MAX_ARTIFACT or total > MAX_ARTIFACT:
                raise WorkerError("declared artifacts exceed aggregate limit")
            save("file-" + str(index), path.read_bytes())
            files[-1]["source_relative_path"] = name
        save("exit.json", _json({"exit_code": observed["exit_code"],
                                 "runtime_id": runtime_id}).encode())
        if inbox:
            execution_ok = protocol_execution_result(observed["exit_code"], inbox.state)
        else:
            execution_ok = observed["exit_code"] == 0 if observed["exit_code"] is not None else None
        atomic(output / "manifest.json", {"attempt_id": plan["attempt_id"],
                                          "execution_ok": execution_ok, "files": files})
        return str(output / "manifest.json")

    def read_artifact(self, plan, runtime_id, name):
        """Read only a hash-verified exported file; caller bounds response bytes."""
        receipt = self._read(plan["attempt_id"])
        if receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest():
            raise WorkerError("runtime plan mismatch")
        root = Path(receipt["attempt_path"]) / "artifacts"
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise WorkerError("artifact manifest unavailable")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("attempt_id") != plan["attempt_id"]:
            raise WorkerError("artifact manifest identity mismatch")
        entry = next((item for item in manifest.get("files", []) if item.get("path") == name), None)
        if entry is None or not re.fullmatch(r"(?:output\.txt|exit\.json|changes\.patch|status\.txt|file-[0-9]{1,2})", name):
            raise WorkerError("undeclared exported artifact")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT:
            raise WorkerError("exported artifact changed or unavailable")
        data = path.read_bytes()
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise WorkerError("exported artifact hash changed")
        return data
