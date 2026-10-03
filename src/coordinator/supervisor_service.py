"""Independent local supervisor process; no Workbench or coordinator database.

Usage (with private operator configuration, never committed):
  python -m coordinator.supervisor_service serve --state-root DIR --config FILE
  python -m coordinator.supervisor_service owner-stop --socket DIR/owner.sock ATTEMPT OPERATION
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import sys

from .docker_runtime import DockerRuntime
from .supervisor import Conflict, Fenced, OwnershipUnknown, Supervisor, WatchdogUncertain


MAX_REQUEST = 131_072
CONTROL = {"acquire", "renew", "launch", "reconcile", "cancel", "inspect"}
OWNER = {"owner_stop", "inspect", "takeover"}


def _private_file(path):
    path = Path(path).absolute()
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or
            path.stat().st_mode & 0o077):
        raise ValueError("operator config must be a caller-owned private file")
    return path


def load_service(state_root, config_file, *, command_fn=None):
    config = json.loads(_private_file(config_file).read_text())
    if set(config) != {"profiles", "workspaces"}:
        raise ValueError("unsupported supervisor configuration")
    arguments = dict(profiles=config["profiles"], workspaces=config["workspaces"])
    if command_fn is not None:
        arguments["command_fn"] = command_fn
    runtime = DockerRuntime(state_root, **arguments)
    return Supervisor(state_root, runtime)


class SupervisorService:
    def __init__(self, supervisor: Supervisor):
        self.supervisor = supervisor
        self.last_watchdog_error = None

    def dispatch(self, request: dict, *, owner=False):
        if not isinstance(request, dict) or set(request) != {"method", "args"} or not isinstance(request["args"], dict):
            raise ValueError("invalid supervisor request")
        method = request["method"]
        if method not in (OWNER if owner else CONTROL):
            raise PermissionError("method unavailable on this socket")
        if not owner and method == "acquire" and request["args"].get("owner_takeover"):
            raise PermissionError("control socket cannot revoke a live lease")
        if owner and method == "takeover":
            return self.supervisor.acquire(**request["args"], owner_takeover=True)
        return getattr(self.supervisor, method)(**request["args"])

    async def _handle(self, reader, writer, *, owner):
        try:
            peer = writer.get_extra_info("socket")
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != os.getuid():
                raise PermissionError("local owner UID required")
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
            if len(line) > MAX_REQUEST:
                raise ValueError("oversized supervisor request")
            request = json.loads(line)
            result = await asyncio.to_thread(self.dispatch, request, owner=owner)
            response = {"ok": True, "result": result}
        except (ValueError, TypeError, KeyError, PermissionError, Conflict,
                OwnershipUnknown, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as exc:
            response = {"ok": False, "error": type(exc).__name__}
        except Exception:
            # No guessed success after Docker or storage failure.
            response = {"ok": False, "error": "Unavailable"}
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _watchdog(self):
        while True:
            try:
                await asyncio.to_thread(self.supervisor.tick)
                self.last_watchdog_error = None
            except WatchdogUncertain as exc:
                state = {"stopped": exc.stopped, "uncertain": exc.uncertain}
                if state != self.last_watchdog_error:
                    print("supervisor watchdog: exact stop uncertain", file=sys.stderr)
                self.last_watchdog_error = state
            except Exception:
                state = {"error": "unavailable"}
                if state != self.last_watchdog_error:
                    print("supervisor watchdog: journal or runtime unavailable", file=sys.stderr)
                self.last_watchdog_error = state
            await asyncio.sleep(0.25)

    async def serve(self):
        root = self.supervisor.root
        endpoints = ((root / "control.sock", False), (root / "owner.sock", True))
        for path, _ in endpoints:
            if path.exists() or path.is_symlink():
                raise ValueError("supervisor socket path already exists; inspect previous service")
        servers = []
        socket_inodes = {}
        try:
            for path, owner in endpoints:
                server = await asyncio.start_unix_server(
                    lambda reader, writer, owner=owner: self._handle(reader, writer, owner=owner),
                    path=str(path), limit=MAX_REQUEST + 1)
                path.chmod(0o600)
                socket_inodes[path] = path.stat().st_ino
                servers.append(server)
            watchdog = asyncio.create_task(self._watchdog())
            try:
                await asyncio.gather(*(server.serve_forever() for server in servers))
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()
            for path, _ in endpoints:
                if path.is_socket() and path.stat().st_ino == socket_inodes.get(path):
                    path.unlink()


def owner_stop(socket_path, attempt_id, operation_id):
    """Emergency local client; response never guesses termination."""
    wire = json.dumps({"method": "owner_stop", "args": {
        "attempt_id": attempt_id, "operation_id": operation_id}}, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(socket_path)
        connection.sendall(wire)
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(MAX_REQUEST))
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--state-root", required=True)
    serve.add_argument("--config", required=True)
    stop = sub.add_parser("owner-stop")
    stop.add_argument("--socket", required=True)
    stop.add_argument("attempt_id")
    stop.add_argument("operation_id")
    args = parser.parse_args()
    if args.action == "serve":
        asyncio.run(SupervisorService(load_service(args.state_root, args.config)).serve())
    else:
        result = owner_stop(args.socket, args.attempt_id, args.operation_id)
        print(json.dumps(result, sort_keys=True))
        if not result.get("ok") or result["result"]["state"] != "stopped":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
