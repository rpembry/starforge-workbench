"""Independent local service routing; no Docker daemon or live socket required."""

import json
import asyncio
import errno

import pytest

from coordinator.supervisor import Supervisor
from coordinator.supervisor_service import SupervisorService, load_service


class Runtime:
    def __init__(self):
        self.items = {}

    def validate(self, plan):
        pass

    def launch(self, plan):
        identity = "runtime-" + plan["attempt_id"]
        self.items[identity] = {"identity_ok": True, "runtime_id": identity,
                                "running": True, "stopped": False, "exit_code": None}
        return identity

    def inspect(self, plan, runtime_id):
        return dict(self.items[runtime_id])

    def stop(self, plan, runtime_id):
        self.items[runtime_id].update(running=False, stopped=True, exit_code=143)


def plan():
    return {"job_id": "job", "attempt_id": "a" * 32, "incarnation": "inc",
            "worker_type": "command", "profile_ref": "offline", "workspace_ref": "scratch",
            "payload": {"argv": ["true"]}, "deadline_seconds": 30,
            "orphan_policy": {"mode": "strict", "grace_seconds": 2, "max_orphan_seconds": 0}}


def test_owner_socket_is_narrow_and_independent(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    service = SupervisorService(Supervisor(root, Runtime()))
    lease = service.dispatch({"method": "acquire", "args": {"controller": "coordinator"}})
    service.dispatch({"method": "launch", "args": {
        "plan": plan(), "controller": "coordinator", "generation": lease["generation"],
        "operation_id": "launch-1"}})
    with pytest.raises(PermissionError):
        service.dispatch({"method": "owner_stop", "args": {
            "attempt_id": "a" * 32, "operation_id": "owner-1"}})
    with pytest.raises(PermissionError):
        service.dispatch({"method": "acquire", "args": {
            "controller": "other", "owner_takeover": True}})
    with pytest.raises(PermissionError):
        service.dispatch({"method": "launch", "args": {}}, owner=True)
    stopped = service.dispatch({"method": "owner_stop", "args": {
        "attempt_id": "a" * 32, "operation_id": "owner-1"}}, owner=True)
    assert stopped["state"] == "stopped"
    assert service.dispatch({"method": "inspect", "args": {"attempt_id": "a" * 32}}, owner=True)["cancel"] == 1


def test_private_operator_config_required(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"profiles": {}, "workspaces": {}}))
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_service(root, config)


def test_local_control_and_owner_sockets(tmp_path):
    async def exercise():
        root = tmp_path / "private"
        root.mkdir(mode=0o700)
        service = SupervisorService(Supervisor(root, Runtime()))
        task = asyncio.create_task(service.serve())
        try:
            for _ in range(50):
                if (root / "control.sock").exists() and (root / "owner.sock").exists():
                    break
                if task.done():
                    try:
                        await task
                    except PermissionError as exc:
                        if exc.errno == errno.EPERM:
                            pytest.skip("sandbox forbids binding Unix sockets")
                        raise
                await asyncio.sleep(0.01)
            async def call(path, method, args):
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write((json.dumps({"method": method, "args": args}) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                return response
            lease = await call(root / "control.sock", "acquire", {"controller": "client"})
            assert lease["ok"]
            launched = await call(root / "control.sock", "launch", {
                "plan": plan(), "controller": "client", "generation": lease["result"]["generation"],
                "operation_id": "launch-1"})
            assert launched["ok"]
            denied = await call(root / "control.sock", "owner_stop", {
                "attempt_id": "a" * 32, "operation_id": "owner-1"})
            assert denied == {"ok": False, "error": "PermissionError"}
            stopped = await call(root / "owner.sock", "owner_stop", {
                "attempt_id": "a" * 32, "operation_id": "owner-1"})
            assert stopped["ok"] and stopped["result"]["state"] == "stopped"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
