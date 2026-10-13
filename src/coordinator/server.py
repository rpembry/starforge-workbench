"""Standalone local-only coordinator service launcher.

The socket lives inside an existing owner-private directory. This process does not
start workers; attaching a reviewed supervisor dispatcher is a separate gate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import stat
import threading

import uvicorn

from . import CoordinatorStore, Policy
from .api import create_app
from .dispatch import Dispatcher, SupervisorControl, SupervisorEvidence


def private_dir(path: Path) -> Path:
    resolved = path.absolute()
    if (not resolved.is_dir() or resolved.is_symlink() or
            resolved.stat().st_uid != os.getuid() or resolved.stat().st_mode & 0o077):
        raise ValueError("directory must already exist, be owner-only, and not be a symlink")
    return resolved


def _dispatch_loop(dispatcher: Dispatcher, stop: threading.Event):
    while not stop.is_set():
        try:
            dispatcher.tick()
            dispatcher.last_error = None
        except Exception as exc:
            # The durable command remains pending/unknown for exact replay.
            # Do not print payloads, paths, or raw supervisor responses.
            dispatcher.last_error = type(exc).__name__
        stop.wait(0.5)


def run(state_root: Path, socket_path: Path, policy_path: Path,
        supervisor_socket: Path | None = None) -> None:
    state_root = private_dir(state_root)
    socket_path = socket_path.absolute()
    private_dir(socket_path.parent)
    if socket_path.exists() or socket_path.is_symlink():
        raise ValueError("socket path already exists; refusing to replace it")
    if (not policy_path.is_file() or policy_path.is_symlink() or
            policy_path.stat().st_uid != os.getuid() or policy_path.stat().st_mode & 0o077):
        raise ValueError("policy file must be caller-owned and private")
    policy = Policy.model_validate_json(policy_path.read_bytes())
    store = CoordinatorStore(state_root, policy)
    dispatcher = (Dispatcher(store, SupervisorControl(supervisor_socket))
                  if supervisor_socket is not None else None)
    app = create_app(store, adapter=SupervisorEvidence(dispatcher) if dispatcher else None)
    parent_fd = os.open(socket_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    listener = None
    socket_inode = None
    stop = threading.Event()
    thread = None
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_mask = os.umask(0o177)
        try:
            # A short fd-relative alias works even when the private state path
            # exceeds Linux's AF_UNIX sockaddr length.
            listener.bind(f"/proc/self/fd/{parent_fd}/{socket_path.name}")
        finally:
            os.umask(old_mask)
        socket_inode = socket_path.stat().st_ino
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        listener.listen(128)
        if dispatcher is not None:
            thread = threading.Thread(target=_dispatch_loop, args=(dispatcher, stop), daemon=True)
            thread.start()
        uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[listener])
    finally:
        stop.set()
        try:
            if thread is not None:
                thread.join(timeout=6)
        finally:
            try:
                if listener is not None:
                    listener.close()
            finally:
                try:
                    if (socket_inode is not None and socket_path.is_socket() and
                            socket_path.stat().st_uid == os.getuid() and
                            socket_path.stat().st_ino == socket_inode):
                        socket_path.unlink()
                finally:
                    os.close(parent_fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="coord-server", description="Owner-only Unix socket coordinator API")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--supervisor-socket", type=Path,
                        help="protected supervisor control.sock; enables durable dispatch")
    args = parser.parse_args(argv)
    try:
        run(args.state_root, args.socket, args.policy, args.supervisor_socket)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
