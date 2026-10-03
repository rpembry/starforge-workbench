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

import uvicorn

from . import CoordinatorStore, Policy
from .api import create_app


def private_dir(path: Path) -> Path:
    resolved = path.absolute()
    if (not resolved.is_dir() or resolved.is_symlink() or
            resolved.stat().st_uid != os.getuid() or resolved.stat().st_mode & 0o077):
        raise ValueError("directory must already exist, be owner-only, and not be a symlink")
    return resolved


def run(state_root: Path, socket_path: Path, policy_path: Path) -> None:
    state_root = private_dir(state_root)
    socket_path = socket_path.absolute()
    private_dir(socket_path.parent)
    if socket_path.exists() or socket_path.is_symlink():
        raise ValueError("socket path already exists; refusing to replace it")
    if (not policy_path.is_file() or policy_path.is_symlink() or
            policy_path.stat().st_uid != os.getuid() or policy_path.stat().st_mode & 0o077):
        raise ValueError("policy file must be caller-owned and private")
    policy = Policy.model_validate_json(policy_path.read_bytes())
    app = create_app(CoordinatorStore(state_root, policy))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_mask = os.umask(0o177)
    try:
        listener.bind(str(socket_path))
    finally:
        os.umask(old_mask)
    try:
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
        listener.listen(128)
        uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[listener])
    finally:
        listener.close()
        if socket_path.is_socket() and socket_path.stat().st_uid == os.getuid():
            socket_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="coord-server", description="Owner-only Unix socket coordinator API")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run(args.state_root, args.socket, args.policy)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
