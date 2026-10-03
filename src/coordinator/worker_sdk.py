"""Minimal worker-side sender. Copy this module into reviewed worker images.

No Workbench or Docker import is needed in the image. The caller supplies one
attempt's channel and writable state file through host-owned mounts.
"""

import json
import hashlib
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
import uuid

MAX_FRAME = 16_384  # independently versioned wire constant; SDK uses stdlib only


class WorkerClient:
    def __init__(self, socket_path: str, state_path: str, *, job_id: str,
                 attempt_id: str, incarnation: str):
        self.socket_path = socket_path
        self.state_path = Path(state_path)
        self.identity = {"job_id": job_id, "attempt_id": attempt_id,
                         "incarnation": incarnation}
        if self.state_path.is_symlink():
            raise ValueError("worker state symlink refused")
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state["identity"] != self.identity:
                raise ValueError("worker state belongs to another incarnation")
        else:
            self.state = {"identity": self.identity, "seq": 0, "pending": None}

    def _save(self):
        fd, path = tempfile.mkstemp(prefix=".worker-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.state_path)
            directory_fd = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def send(self, kind: str, data: dict, *, max_wait_seconds=30):
        if self.state["pending"] is not None:
            raise RuntimeError("resolve pending message before sending another")
        self.state["pending"] = {"protocol": "worker.v1", **self.identity,
                                 "seq": self.state["seq"] + 1,
                                 "event_id": uuid.uuid4().hex, "kind": kind, "data": data}
        self._save()  # preserve exact event identity before network write
        return self.retry_pending(max_wait_seconds=max_wait_seconds)

    def retry_pending(self, *, max_wait_seconds=30):
        pending = self.state["pending"]
        if pending is None:
            return None
        if not 0 <= max_wait_seconds <= 300:
            raise ValueError("invalid reconnect budget")
        wire = (json.dumps(pending, separators=(",", ":")) + "\n").encode()
        if len(wire) > MAX_FRAME:
            raise ValueError("worker message exceeds frame limit")
        deadline = time.monotonic() + max_wait_seconds
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(5)
                    connection.connect(self.socket_path)
                    connection.sendall(wire)
                    with connection.makefile("rb") as stream:
                        response = json.loads(stream.readline(MAX_FRAME))
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise ConnectionError("worker channel unavailable; pending frame retained") from None
                time.sleep(min(0.25, deadline - time.monotonic()))
        if response.get("status") not in {"accepted", "duplicate"} or response.get("ack_seq") < pending["seq"]:
            raise RuntimeError("worker message not acknowledged")
        self.state["seq"] = pending["seq"]
        self.state["pending"] = None
        self._save()
        return response


class MailboxWorkerClient(WorkerClient):
    """Single-slot file sender for the fixed offline Docker worker.

    The host's private inbox is authoritative. Mailbox acknowledgements only
    let this sender advance its own durable sequence; they are not host result
    evidence and cannot authorize supervisor actions.
    """

    REQUEST = "request.json"
    ACK = "ack.json"
    MAX_ACK = 512

    def __init__(self, channel_dir: str, state_path: str, *, job_id: str,
                 attempt_id: str, incarnation: str):
        super().__init__(channel_dir, state_path, job_id=job_id,
                         attempt_id=attempt_id, incarnation=incarnation)
        self.channel_dir = Path(channel_dir).absolute()
        if self.channel_dir.is_symlink() or not self.channel_dir.is_dir():
            raise ValueError("worker mailbox directory unavailable")

    def _directory_fd(self):
        return os.open(self.channel_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _publish(self, wire):
        fd, path = tempfile.mkstemp(prefix=".request-", dir=self.channel_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(wire)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = self._directory_fd()
            try:
                os.replace(Path(path).name, self.REQUEST,
                           src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _read_ack(self, pending, digest):
        directory_fd = self._directory_fd()
        try:
            try:
                fd = os.open(self.ACK, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError:
                return None  # hostile or unreadable ack cannot block publication
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_uid != os.getuid() or info.st_mode & 0o077 or
                        info.st_size > self.MAX_ACK):
                    return None
                data = os.read(fd, self.MAX_ACK + 1)
                if len(data) != info.st_size:
                    return None
            finally:
                os.close(fd)
            try:
                ack = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return None
            if (not isinstance(ack, dict) or set(ack) != {
                    "seq", "event_id", "frame_sha256", "response"} or
                    ack["seq"] != pending["seq"] or
                    ack["event_id"] != pending["event_id"] or
                    ack["frame_sha256"] != digest or not isinstance(ack["response"], dict)):
                return None
            return info.st_ino, ack["response"]
        finally:
            os.close(directory_fd)

    def _remove_ack(self, inode):
        directory_fd = self._directory_fd()
        try:
            try:
                current = os.stat(self.ACK, dir_fd=directory_fd, follow_symlinks=False)
                if current.st_ino == inode and stat.S_ISREG(current.st_mode):
                    os.unlink(self.ACK, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory_fd)

    def retry_pending(self, *, max_wait_seconds=30):
        pending = self.state["pending"]
        if pending is None:
            return None
        if not 0 <= max_wait_seconds <= 300:
            raise ValueError("invalid reconnect budget")
        wire = (json.dumps(pending, separators=(",", ":")) + "\n").encode()
        if len(wire) > MAX_FRAME:
            raise ValueError("worker message exceeds frame limit")
        digest = hashlib.sha256(wire).hexdigest()
        deadline = time.monotonic() + max_wait_seconds
        last_publish = None
        while True:
            try:
                matched = self._read_ack(pending, digest)
                if matched is not None:
                    inode, response = matched
                    if (response.get("status") not in {"accepted", "duplicate"} or
                            type(response.get("ack_seq")) is not int or
                            response["ack_seq"] < pending["seq"]):
                        raise RuntimeError("worker message not acknowledged")
                    self.state["seq"] = pending["seq"]
                    self.state["pending"] = None
                    self._save()
                    self._remove_ack(inode)
                    return response
                if last_publish is None or time.monotonic() - last_publish >= 0.5:
                    self._publish(wire)
                    last_publish = time.monotonic()
            except OSError:
                pass  # channel unavailable; preserve the exact pending frame
            if time.monotonic() >= deadline:
                raise ConnectionError("worker mailbox unavailable; pending frame retained")
            time.sleep(min(0.05, deadline - time.monotonic()))


def command_evidence(*, running: bool, stopped: bool, exit_code: int | None):
    """Ordinary-command adapter evidence; readiness is explicitly synthetic."""
    if running:
        return {"ready": True, "ready_source": "command_adapter_started",
                "progress": None, "result": None}
    if not stopped or exit_code is None:
        return {"ready": False, "ready_source": "unknown", "progress": None, "result": None}
    return {"ready": False, "ready_source": "command_adapter_started", "progress": None,
            "result": {"ok": exit_code == 0, "source": "confirmed_process_exit", "exit_code": exit_code}}
