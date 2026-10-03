import asyncio
import errno
import os
import socket

import pytest

from coordinator.worker_protocol import WorkerInbox, serve_worker_socket
from coordinator.worker_sdk import WorkerClient, command_evidence
from coordinator.worker_channel import WorkerChannel


def test_socket_sdk_reconnect(tmp_path):
    async def exercise():
        root = tmp_path / "attempt"
        root.mkdir(mode=0o700)
        inbox = WorkerInbox(root, job_id="job", attempt_id="attempt", incarnation="inc")
        try:
            server = await serve_worker_socket(root / "events.sock", inbox)
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                pytest.skip("sandbox forbids binding Unix sockets")
            raise
        try:
            client = WorkerClient(str(root / "events.sock"), str(root / "sender.json"),
                                  job_id="job", attempt_id="attempt", incarnation="inc")
            await asyncio.to_thread(client.send, "hello", {"capabilities": []})
            await asyncio.to_thread(client.send, "ready", {})
            assert inbox.replay(0)["latest"] == 2
            client.state["pending"] = {"protocol": "worker.v1", "job_id": "job",
                                        "attempt_id": "attempt", "incarnation": "inc", "seq": 2,
                                        "event_id": inbox.state["events"][-1]["event_id"],
                                        "kind": "ready", "data": {}}
            client._save()  # simulate lost acknowledgement after host commit
            restarted = WorkerClient(str(root / "events.sock"), str(root / "sender.json"),
                                     job_id="job", attempt_id="attempt", incarnation="inc")
            response = await asyncio.to_thread(restarted.retry_pending)
            assert response["status"] == "duplicate"
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())


def test_command_adapter_truthfulness():
    assert command_evidence(running=True, stopped=False, exit_code=None)["progress"] is None
    assert command_evidence(running=False, stopped=False, exit_code=None)["result"] is None
    assert command_evidence(running=False, stopped=True, exit_code=1)["result"]["ok"] is False


def test_unavailable_channel_keeps_exact_pending_frame(tmp_path):
    root = tmp_path / "attempt"
    root.mkdir(mode=0o700)
    client = WorkerClient(str(root / "missing.sock"), str(root / "sender.json"),
                          job_id="job", attempt_id="attempt", incarnation="inc")
    with pytest.raises(ConnectionError):
        client.send("hello", {"capabilities": []}, max_wait_seconds=0)
    pending = client.state["pending"]
    assert client.state["seq"] == 0 and pending["seq"] == 1
    restarted = WorkerClient(str(root / "missing.sock"), str(root / "sender.json"),
                             job_id="job", attempt_id="attempt", incarnation="inc")
    assert restarted.state["pending"] == pending


def test_long_path_stale_socket_reopens_by_directory_fd(tmp_path):
    root = tmp_path / ("deep-" + "a" * 50)
    root.mkdir(mode=0o700)
    channel_dir = root / "channel"
    channel_dir.mkdir(mode=0o700)
    assert len(str(channel_dir / "events.sock").encode()) > 108
    fd = os.open(channel_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            try:
                stale.bind(f"/proc/self/fd/{fd}/events.sock")
            except PermissionError as exc:
                if exc.errno == errno.EPERM:
                    pytest.skip("sandbox forbids binding Unix sockets")
                raise
    finally:
        os.close(fd)
    recovered = WorkerChannel(root, job_id="job", attempt_id="attempt", incarnation="inc")
    try:
        assert recovered.socket_path.is_socket()
    finally:
        recovered.close()
