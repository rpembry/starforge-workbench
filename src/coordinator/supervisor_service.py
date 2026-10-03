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
from .supervisor import Conflict, Fenced, OwnershipUnknown, RecoveryUncertain, Supervisor, WatchdogUncertain


MAX_REQUEST = 131_072
CONTROL = {"acquire", "renew", "launch", "abandon", "reconcile", "cancel", "collect",
           "read_artifact", "inspect"}
OWNER = {"owner_stop", "inspect", "takeover", "review", "archive"}


def _private_file(path):
    path = Path(path).absolute()
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or
            path.stat().st_mode & 0o077):
        raise ValueError("operator config must be a caller-owned private file")
    return path


def load_service(state_root, config_file, *, command_fn=None):
    config = json.loads(_private_file(config_file).read_text())
    if set(config) not in ({"profiles", "workspaces"}, {"profiles", "workspaces", "host_budget"}):
        raise ValueError("unsupported supervisor configuration")
    arguments = dict(profiles=config["profiles"], workspaces=config["workspaces"])
    if "host_budget" in config:
        arguments["host_budget"] = config["host_budget"]
    if command_fn is not None:
        arguments["command_fn"] = command_fn
    runtime = DockerRuntime(state_root, **arguments)
    runtime.reopen_channels()
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
            recovery_error = None
            try:
                if self.supervisor.recovery_required():
                    await asyncio.to_thread(self.supervisor.recover_startup)
            except RecoveryUncertain as exc:
                recovery_error = {"recovery_uncertain": exc.uncertain}
            except Exception:
                recovery_error = {"recovery_error": "unavailable"}
            try:
                await asyncio.to_thread(self.supervisor.tick)
                self.last_watchdog_error = recovery_error
            except WatchdogUncertain as exc:
                state = {"stopped": exc.stopped, "runtime_uncertain": exc.runtime_uncertain,
                         "readiness_uncertain": exc.readiness_uncertain}
                if state != self.last_watchdog_error:
                    if exc.runtime_uncertain:
                        print("supervisor watchdog: runtime observation or exact stop uncertain",
                              file=sys.stderr)
                    if exc.readiness_uncertain:
                        print("supervisor watchdog: readiness observation uncertain",
                              file=sys.stderr)
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
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for path, owner in endpoints:
                server = await asyncio.start_unix_server(
                    lambda reader, writer, owner=owner: self._handle(reader, writer, owner=owner),
                    path=f"/proc/self/fd/{root_fd}/{path.name}", limit=MAX_REQUEST + 1)
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
            close_channels = getattr(self.supervisor.runtime, "close_channels", None)
            if close_channels is not None:
                close_channels()
            os.close(root_fd)


def owner_call(socket_path, method, args):
    """Local owner client; the service returns confirmed evidence or an error."""
    if method not in {"owner_stop", "review", "archive"}:
        raise ValueError("unsupported owner command")
    wire = json.dumps({"method": method, "args": args}, separators=(",", ":")).encode() + b"\n"
    path = Path(socket_path).absolute()
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(f"/proc/self/fd/{directory_fd}/{path.name}")
            connection.sendall(wire)
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(MAX_REQUEST))
    finally:
        os.close(directory_fd)
    return response


def owner_stop(socket_path, attempt_id, operation_id):
    """Emergency local stop; response never guesses termination."""
    return owner_call(socket_path, "owner_stop", {
        "attempt_id": attempt_id, "operation_id": operation_id})


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
    review = sub.add_parser("review")
    review.add_argument("--socket", required=True)
    review.add_argument("attempt_id")
    archive = sub.add_parser("archive")
    archive.add_argument("--socket", required=True)
    archive.add_argument("attempt_id")
    archive.add_argument("review_sha256")
    archive.add_argument("operation_id")
    args = parser.parse_args()
    if args.action == "serve":
        asyncio.run(SupervisorService(load_service(args.state_root, args.config)).serve())
    elif args.action == "owner-stop":
        result = owner_stop(args.socket, args.attempt_id, args.operation_id)
        print(json.dumps(result, sort_keys=True))
        if not result.get("ok") or result["result"]["state"] != "stopped":
            raise SystemExit(2)
    elif args.action == "review":
        result = owner_call(args.socket, "review", {"attempt_id": args.attempt_id})
        print(json.dumps(result, sort_keys=True))
        if not result.get("ok"):
            raise SystemExit(2)
    else:
        result = owner_call(args.socket, "archive", {
            "attempt_id": args.attempt_id, "review_sha256": args.review_sha256,
            "operation_id": args.operation_id})
        print(json.dumps(result, sort_keys=True))
        if not result.get("ok") or result["result"]["phase"] != "complete":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
