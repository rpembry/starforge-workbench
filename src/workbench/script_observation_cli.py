"""Opt-in local wrapper/status CLI for one protected external-script descriptor."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

from .script_observation import ScriptIdentity, run_observed
from .script_observation_local import status_from_spool, write_event

MAX_DESCRIPTOR_BYTES = 8192


@dataclass(frozen=True)
class LocalDescriptor:
    identity: ScriptIdentity
    argv: tuple[str, ...]
    directory: Path
    cwd: str | None


def load_descriptor(path: Path) -> LocalDescriptor:
    """Read a caller-owned 0600 file; never echo command or path values."""
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("unsafe_script_descriptor")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_DESCRIPTOR_BYTES):
            raise ValueError("unsafe_script_descriptor")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(raw) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("oversized_script_descriptor")
    finally:
        os.close(fd)
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"version", "identity", "argv", "spool_dir", "cwd"}:
        raise ValueError("invalid_script_descriptor")
    if value["version"] != 1 or not isinstance(value["identity"], dict) or set(value["identity"]) != {
            "script_id", "revision", "adapter_id"}:
        raise ValueError("invalid_script_descriptor")
    identity = ScriptIdentity(**value["identity"])
    command = value["argv"]
    if (not isinstance(command, list) or not 1 <= len(command) <= 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 2048 for arg in command)):
        raise ValueError("invalid_script_descriptor")
    directory = value["spool_dir"]
    cwd = value["cwd"]
    if (not isinstance(directory, str) or not Path(directory).is_absolute()
            or (cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()))):
        raise ValueError("invalid_script_descriptor")
    return LocalDescriptor(identity, tuple(command), Path(directory), cwd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        descriptor = load_descriptor(args.config)
    except (OSError, ValueError, TypeError, KeyError):
        print("script_observation_config_error", file=sys.stderr)
        return 2
    if args.action == "run":
        return run_observed(descriptor.identity, descriptor.argv,
                            lambda event: write_event(descriptor.directory, descriptor.identity, event),
                            cwd=descriptor.cwd)
    status = status_from_spool(descriptor.directory, descriptor.identity,
                               now=datetime.now(timezone.utc), freshness_seconds=300)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
