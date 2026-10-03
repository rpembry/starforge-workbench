"""Minimal worker-side sender. Copy this module into reviewed worker images.

No Workbench or Docker import is needed in the image. The caller supplies one
attempt's socket and writable state file through host-owned mounts.
"""

import json
import os
from pathlib import Path
import socket
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


def command_evidence(*, running: bool, stopped: bool, exit_code: int | None):
    """Ordinary-command adapter evidence; readiness is explicitly synthetic."""
    if running:
        return {"ready": True, "ready_source": "command_adapter_started",
                "progress": None, "result": None}
    if not stopped or exit_code is None:
        return {"ready": False, "ready_source": "unknown", "progress": None, "result": None}
    return {"ready": False, "ready_source": "command_adapter_started", "progress": None,
            "result": {"ok": exit_code == 0, "source": "confirmed_process_exit", "exit_code": exit_code}}
