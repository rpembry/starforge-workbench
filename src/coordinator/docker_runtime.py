"""Host-owned scratch Docker adapter for the supervisor protocol.

This reuses the established runner's restricted Docker create/verify functions.
It never adopts legacy receipts, pulls an image, or accepts host paths from a job.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from starforge_workbench.docker_worker import (
    MAX_ARTIFACT, WorkerError, atomic, command, create_argv, docker_prefix,
    environment_digest, private_directory, verify_container,
)
from starforge_workbench.execution import parse_profile

from .store import _json
from .worker_channel import WorkerChannel
from .worker_protocol import WorkerInbox


LABEL = "io.starforge.coordinator"


class DockerRuntime:
    """One local Docker host, with approved immutable refs supplied by operator.

    `profiles` maps names to already reviewed Docker profile mappings.
    `workspaces` maps names to the literal `scratch`; other workspace adapters
    are rejected until separately implemented. `worker_types` is a set of
    registered host adapters: ordinary `command` and fixed `protocol_example`.
    """

    def __init__(self, state_root, *, profiles: dict, workspaces: dict,
                 worker_types=frozenset({"command", "protocol_example"}), command_fn=command):
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
        self.worker_types = frozenset(worker_types)
        self.command = command_fn
        self.docker = docker_prefix(False)  # no implicit sudo or remote context
        self.channels = {}

    def validate(self, plan):
        if plan["profile_ref"] not in self.profiles:
            raise ValueError("unapproved Docker profile")
        if self.workspaces.get(plan["workspace_ref"]) != "scratch":
            raise ValueError("unsupported or unapproved workspace adapter")
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
        workspace.mkdir(mode=0o700)
        (attempt / "scratch").mkdir(mode=0o700)
        (attempt / "git-mask").write_text("No container Git administration.\n")
        (attempt / "git-mask").chmod(0o444)
        if "input" in plan["payload"]:
            (workspace / "job.json").write_text(_json(plan["payload"]["input"]) + "\n")
            (workspace / "job.json").chmod(0o444)
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
                   "phase": "create_pending"}
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
        receipt = self._read(plan["attempt_id"])
        if (receipt["plan_hash"] != hashlib.sha256(_json(plan).encode()).hexdigest() or
                receipt["incarnation"] != plan["incarnation"] or
                runtime_id and receipt["container_id"] and runtime_id != receipt["container_id"]):
            raise WorkerError("runtime plan or incarnation mismatch")
        item = self._inspect(receipt)
        if item is None:
            raise WorkerError("owned container absent without exit evidence")
        status = item["State"]["Status"]
        return {"identity_ok": True, "running": bool(item["State"]["Running"]),
                "stopped": status in {"exited", "dead", "created"},
                "exit_code": None if status == "created" else item["State"]["ExitCode"],
                "runtime_id": item["Id"]}

    def stop(self, plan, runtime_id):
        observed = self.inspect(plan, runtime_id)
        if not observed["stopped"]:
            self.command(self.docker + ["stop", "--time", "5", observed["runtime_id"]], timeout=15)
            if not self.inspect(plan, observed["runtime_id"])["stopped"]:
                raise WorkerError("stop not confirmed")

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
            result = inbox.state["result"]
            execution_ok = (observed["exit_code"] == 0 and inbox.state["ready"] and
                            result is not None and result["data"]["ok"] is True)
            if result is None or not inbox.state["ready"] or observed["exit_code"] is None:
                execution_ok = None
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
        if entry is None or not re.fullmatch(r"(?:output\.txt|exit\.json|file-[0-9]{1,2})", name):
            raise WorkerError("undeclared exported artifact")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT:
            raise WorkerError("exported artifact changed or unavailable")
        data = path.read_bytes()
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise WorkerError("exported artifact hash changed")
        return data
