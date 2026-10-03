import asyncio
import errno

import pytest

from coordinator.worker_protocol import WorkerInbox, serve_worker_socket
from coordinator.worker_sdk import WorkerClient, command_evidence


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
