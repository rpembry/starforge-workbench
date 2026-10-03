"""Worker v1 NDJSON validation and durable bounded per-attempt inbox.

The socket carries worker evidence only. It cannot invoke the supervisor's
owner or Docker methods. Host artifact validation remains a separate step.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_FRAME = 16_384
MAX_EVENTS = 128
MAX_REPLAY_BYTES = 1_048_576
KINDS = {"hello", "ready", "heartbeat", "progress", "log", "result", "artifact"}


class ProtocolError(ValueError):
    pass


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: str = "worker.v1"
    job_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    incarnation: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=1)
    event_id: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,128}$")
    kind: str
    data: dict

    @field_validator("protocol")
    @classmethod
    def protocol_version(cls, value):
        if value != "worker.v1":
            raise ValueError("unsupported worker protocol")
        return value

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind not in KINDS:
            raise ValueError("unsupported message type")
        if len(json.dumps(self.data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()) > 8192:
            raise ValueError("message data too large")
        if self.kind == "hello":
            if set(self.data) != {"capabilities"} or not isinstance(self.data["capabilities"], list):
                raise ValueError("hello needs capabilities")
            if any(cap not in {"checkpoint"} for cap in self.data["capabilities"]):
                raise ValueError("unsupported capability")
        elif self.kind == "progress":
            if set(self.data) != {"completed", "total"} or not (
                    isinstance(self.data["completed"], int) and isinstance(self.data["total"], int) and
                    0 <= self.data["completed"] <= self.data["total"] <= 1_000_000):
                raise ValueError("invalid progress")
        elif self.kind == "log":
            if set(self.data) != {"level", "message"} or self.data["level"] not in {
                    "debug", "info", "warning", "error"} or not isinstance(self.data["message"], str):
                raise ValueError("invalid log metadata")
        elif self.kind == "result":
            if set(self.data) != {"ok", "summary"} or type(self.data["ok"]) is not bool or not isinstance(self.data["summary"], str):
                raise ValueError("invalid result declaration")
        elif self.kind == "artifact":
            path = self.data.get("path")
            if set(self.data) != {"path"} or not isinstance(path, str) or not path or len(path) > 256 or (
                    Path(path).is_absolute() or any(part in {"..", ".", ".git"} for part in Path(path).parts) or
                    "\x00" in path):
                raise ValueError("invalid artifact declaration")
        elif self.data:
            raise ValueError("message data must be empty")
        return self


def decode_frame(line: bytes) -> Frame:
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ProtocolError("oversized or incomplete worker frame")
    try:
        value = json.loads(line)
        return Frame.model_validate(value)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("malformed worker frame") from exc


class WorkerInbox:
    """One bound attempt, with fsynced sequence/result before acknowledgement."""

    def __init__(self, directory: str | Path, *, job_id: str, attempt_id: str, incarnation: str):
        self.directory = Path(directory).absolute()
        if (not self.directory.is_dir() or self.directory.is_symlink() or
                self.directory.stat().st_uid != os.getuid() or self.directory.stat().st_mode & 0o077):
            raise ValueError("worker inbox directory must be private and caller-owned")
        self.path = self.directory / "worker-inbox.json"
        self.identity = {"job_id": job_id, "attempt_id": attempt_id, "incarnation": incarnation}
        if self.path.is_symlink():
            raise ValueError("worker inbox symlink refused")
        if self.path.exists():
            if self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077:
                raise ValueError("worker inbox must be private and caller-owned")
            self.state = json.loads(self.path.read_text())
            if self.state["identity"] != self.identity:
                raise ProtocolError("persisted worker incarnation mismatch")
            self.state.setdefault("artifacts", [])
        else:
            self.state = {"identity": self.identity, "last_seq": 0, "hello": False,
                          "ready": False, "result": None, "events": [], "floor": 1,
                          "gaps": [], "artifacts": []}

    def _save(self):
        fd, path = tempfile.mkstemp(prefix=".inbox-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def accept(self, frame: Frame):
        if any(getattr(frame, key) != value for key, value in self.identity.items()):
            raise ProtocolError("cross-attempt or stale-incarnation message")
        body = frame.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        last = self.state["last_seq"]
        if frame.seq <= last:
            prior = next((entry for entry in self.state["events"] if entry["seq"] == frame.seq), None)
            if prior and prior["event_id"] == frame.event_id and prior["digest"] == digest:
                return {"status": "duplicate", "ack_seq": last, "floor": self.state["floor"]}
            raise ProtocolError("stale or conflicting worker sequence")
        if any(entry["event_id"] == frame.event_id for entry in self.state["events"]):
            raise ProtocolError("event ID reused")
        if not self.state["hello"]:
            if frame.kind != "hello" or frame.seq != 1:
                raise ProtocolError("worker must handshake first")
        elif frame.kind == "hello":
            raise ProtocolError("duplicate handshake")
        if self.state["result"] is not None and frame.kind in {"result", "progress", "artifact"}:
            raise ProtocolError("result already declared")
        if frame.kind == "artifact" and len(self.state["artifacts"]) >= 32:
            raise ProtocolError("too many artifact declarations")
        gap = [last + 1, frame.seq - 1] if frame.seq > last + 1 else None
        previous = self.state
        self.state = json.loads(json.dumps(previous))
        try:
            if gap:
                self.state["gaps"].append(gap)
                self.state["gaps"] = self.state["gaps"][-MAX_EVENTS:]
            if frame.kind == "hello":
                self.state["hello"] = True
            elif frame.kind == "ready":
                self.state["ready"] = True
            elif frame.kind == "result":
                self.state["result"] = body
            elif frame.kind == "artifact":
                if frame.data["path"] in self.state["artifacts"]:
                    raise ProtocolError("artifact declared twice")
                self.state["artifacts"].append(frame.data["path"])
            self.state["last_seq"] = frame.seq
            self.state["events"].append({"seq": frame.seq, "event_id": frame.event_id,
                                         "digest": digest, "frame": body})
            while (len(self.state["events"]) > MAX_EVENTS or
                   len(json.dumps(self.state["events"]).encode()) > MAX_REPLAY_BYTES):
                self.state["events"].pop(0)
            self.state["floor"] = self.state["events"][0]["seq"]
            self._save()
        except BaseException:
            self.state = previous
            raise
        return {"status": "accepted", "ack_seq": frame.seq, "floor": self.state["floor"],
                "gap": gap}

    def replay(self, after_seq: int):
        if after_seq < 0:
            raise ValueError("invalid cursor")
        return {"events": [entry["frame"] for entry in self.state["events"] if entry["seq"] > after_seq],
                "floor": self.state["floor"], "gap": after_seq < self.state["floor"] - 1,
                "latest": self.state["last_seq"], "sequence_gaps": self.state["gaps"],
                "result": self.state["result"], "artifacts": self.state["artifacts"]}


async def serve_worker_socket(path: str | Path, inbox: WorkerInbox):
    """Create a per-attempt Unix socket; caller owns service lifecycle and ACLs."""
    socket_path = Path(path)
    if socket_path.parent != inbox.directory or socket_path.exists() or socket_path.is_symlink():
        raise ValueError("socket must be a new path in the bound inbox directory")

    async def handle(reader, writer):
        try:
            while True:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=30)
                if not line:
                    break
                if len(line) > MAX_FRAME:
                    raise ProtocolError("oversized worker frame")
                frame = decode_frame(line)
                ack = inbox.accept(frame)
                writer.write((json.dumps(ack, separators=(",", ":")) + "\n").encode())
                await writer.drain()
        except (ProtocolError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            writer.write(b'{"status":"rejected"}\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=MAX_FRAME + 1)
    socket_path.chmod(0o600)
    return server
